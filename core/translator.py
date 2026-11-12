"""Multi-Provider Translation Engine with Concurrent Workers, Real-Time Streaming, Watchdog Timeout, and Auto-Retry."""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Set, Tuple

from .cache import TranslationCache
from .cost_estimator import (
    PROVIDERS,
    calculate_realtime_cost,
    estimate_text_tokens,
    format_duration_ko,
)
from .glossary import GlossaryManager
from .scanner import ModEntry, ModpackScanner, parse_relaxed_json
from .validator import validate_and_repair_translation


SYSTEM_INSTRUCTION_TEMPLATE = """You are an expert Korean translator for Minecraft modpacks.
Translate the values of the provided JSON object from English to natural Korean suitable for Minecraft players.

CRITICAL RULES:
1. DO NOT call any tools, DO NOT read or write files, DO NOT run commands. Output ONLY raw JSON text directly.
2. Return ONLY a valid JSON object with the EXACT same numeric keys ("0", "1", ...) as the input. No markdown explanations outside JSON.
3. NEVER alter or remove format specifiers such as %s, %d, %1$s, %2$s, %.1f, {0}, {1}. Keep them in their grammatically appropriate position in Korean.
4. NEVER alter or remove Minecraft formatting/color codes such as §a, §6, §l, §r, &a, &b, &6, &r, tags like {@pagebreak}, Patchouli macros like $(item), $(l:...), $(/l), $(br2), $(li), $(thing), or GuideMe JSX/MDX tags like <ItemLink id="..." />, <BlockImage ... />, <RecipeFor ... />.
5. If a value is a serialized JSON array (starts with '[' and contains "text", "keybind", "click_event", etc.), ONLY translate the display text strings inside it while keeping the valid JSON string structure intact.
6. Follow the official Minecraft & Mod Glossary below strictly for terminology consistency.
{glossary_section}"""


BILINGUAL_RULE = """
7. BILINGUAL ANNOTATION (Must Follow): For proper nouns and game terms (item, block, mob, biome, dimension, enchantment, spell, machine and mod-specific term names), write the Korean translation immediately followed by the original English name in parentheses with NO space, e.g. "철 주괴(Iron Ingot)", "엔더맨(Enderman)", "가속(Speed)".
   - Annotate only names/terms. Do NOT annotate ordinary words, verbs or whole sentences.
   - In a long description, annotate a given term only the first time it appears in that value.
   - If the Korean text is identical to the English (e.g. a loanword kept as-is), do not repeat it.
   - Never put the English annotation inside format specifiers, color codes, macros or tags (rule 3 and 4 still apply). Glossary terms are still annotated; use the glossary Korean form followed by the English name.
"""

PAIR_REGEX = re.compile(r'"((?:\\.|[^"\\])*)"\s*:\s*"((?:\\.|[^"\\])*)"')


def extract_json_from_llm_response(raw_output: str) -> Dict[str, str]:
    """
    Robustly extract a JSON dictionary from an LLM response (including partial/streamed JSON).
    Even if the LLM wrapped it in ```json ... ``` or was interrupted mid-stream,
    this function recovers all valid "key": "value" pairs!
    """
    if not raw_output:
        return {}

    text = raw_output.strip()

    # Fast path for complete JSON
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        candidate = fence_match.group(1)
    else:
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
        else:
            candidate = ""

    if candidate:
        parsed = parse_relaxed_json(candidate)
        if parsed and isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if isinstance(v, (str, int, float))}

    # Fallback / Partial stream recovery: extract all complete "key": "value" pairs via regex
    recovered: Dict[str, str] = {}
    for m in PAIR_REGEX.finditer(raw_output):
        try:
            k = json.loads(f'"{m.group(1)}"')
            v = json.loads(f'"{m.group(2)}"')
            recovered[str(k)] = str(v)
        except Exception:
            continue
    return recovered


class MultiProviderTranslator:
    """Executes chunked JSON translation with real-time token streaming, watchdog timeouts, and auto-retry."""

    def __init__(
        self,
        scanner: ModpackScanner,
        glossary: GlossaryManager,
        cache: TranslationCache,
    ):
        self.scanner = scanner
        self.glossary = glossary
        self.cache = cache
        self._cancel_requested = False
        self.bilingual = False  # Append English originals in parentheses (set per job in translate_entries)
        self._active_procs: Set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._tracked_agy_conversation_ids: Set[str] = set()
        self._agy_track_lock = threading.Lock()

        # Dedicated empty sandbox directory so AGY CLI never scans the modpack workspace
        self.agy_sandbox_dir = os.path.join(
            os.path.dirname(self.cache.db_path), "agy_sandbox"
        )
        os.makedirs(self.agy_sandbox_dir, exist_ok=True)

    def request_cancel(self) -> None:
        self._cancel_requested = True
        with self._proc_lock:
            for proc in list(self._active_procs):
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
            self._active_procs.clear()

    def reset_cancel(self) -> None:
        self._cancel_requested = False

    def build_system_prompt(self, mod_id: str, chunk_values: List[str]) -> str:
        glossary_str = self.glossary.format_glossary_for_prompt(mod_id, chunk_values)
        if glossary_str:
            glossary_section = f"\n[GLOSSARY (Must Follow)]\n{glossary_str}\n"
        else:
            glossary_section = ""
        template = SYSTEM_INSTRUCTION_TEMPLATE
        if self.bilingual:
            # Insert the bilingual rule right after rule 6, before the glossary section
            template = template.replace("{glossary_section}", BILINGUAL_RULE + "{glossary_section}")
        return template.replace("{glossary_section}", glossary_section)

    def call_provider(
        self,
        provider_id: str,
        model: str,
        api_key: str,
        mod_id: str,
        chunk_dict: Dict[str, str],
        ollama_url: str = "http://localhost:11434",
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        """Dispatch a single chunk dictionary to the selected translation provider."""
        system_prompt = self.build_system_prompt(mod_id, list(chunk_dict.values()))
        payload_json_str = json.dumps(chunk_dict, ensure_ascii=False, indent=2)

        if provider_id == "agy":
            return self._call_agy_streaming(model, system_prompt, payload_json_str, on_stream_delta)
        elif provider_id in ("gemini_flash", "gemini_lite", "gemini_pro", "gemini_argon"):
            if not api_key and provider_id in ("gemini_pro", "gemini_argon") and (shutil.which("agy") or shutil.which("agy.exe")):
                # Seamless fallback to local AGY CLI if user selected Gemini 3.1 Pro or Argon without an API key
                return self._call_agy_streaming(model, system_prompt, payload_json_str, on_stream_delta)
            return self._call_gemini(model, api_key, system_prompt, payload_json_str, on_stream_delta)
        elif provider_id in ("openai_mini", "openai_4o"):
            return self._call_openai(model, api_key, system_prompt, payload_json_str, on_stream_delta)
        elif provider_id == "claude_haiku":
            return self._call_claude(model, api_key, system_prompt, payload_json_str, on_stream_delta)
        elif provider_id == "ollama":
            return self._call_ollama(model, ollama_url, system_prompt, payload_json_str, on_stream_delta)
        elif provider_id == "deepl":
            return self._call_deepl(model, api_key, chunk_dict, on_stream_delta)
        else:
            raise ValueError(f"지원하지 않는 번역 프로바이더입니다: {provider_id}")

    def _call_agy_streaming(
        self,
        model: str,
        system_prompt: str,
        payload_json_str: str,
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        """
        Run AGY CLI in non-blocking streaming mode (`--input-format stream-json --output-format stream-json`),
        reading token deltas in real time with a heartbeat watchdog timer so it never hangs silently.
        """
        agy_bin = shutil.which("agy") or shutil.which("agy.exe") or "agy"
        full_prompt = (
            f"{system_prompt}\n\n"
            f"[INPUT JSON TO TRANSLATE]\n"
            f"{payload_json_str}\n\n"
            f"Respond ONLY with the translated JSON object (do not use any tools):"
        )

        selected_model = (model or "gemini-3.8-flash-low").strip()
        cmd = [
            agy_bin,
            "--disable-slash-commands",
            "--model",
            selected_model,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "-p",
            "",
        ]
        if not any(selected_model.endswith(sfx) for sfx in ("-low", "-medium", "-high", "-xhigh", "-max")):
            cmd.extend(["--effort", "low"])

        stream_input_line = (
            json.dumps(
                {
                    "event": "user",
                    "message": {
                        "text": full_prompt,
                        "content": full_prompt,
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

        proc = subprocess.Popen(
            cmd,
            cwd=self.agy_sandbox_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._proc_lock:
            self._active_procs.add(proc)

        line_queue: queue.Queue[Optional[str]] = queue.Queue()
        stderr_chunks: List[str] = []

        def stdout_reader():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        line_queue.put(line)
            except Exception:
                pass
            finally:
                line_queue.put(None)

        def stderr_reader():
            try:
                if proc.stderr:
                    stderr_chunks.append(proc.stderr.read())
            except Exception:
                pass

        t_out = threading.Thread(target=stdout_reader, daemon=True)
        t_err = threading.Thread(target=stderr_reader, daemon=True)
        t_out.start()
        t_err.start()

        try:
            if proc.stdin:
                proc.stdin.write(stream_input_line)
                proc.stdin.flush()
                proc.stdin.close()
        except Exception as e:
            proc.kill()
            raise RuntimeError(f"AGY stdin 전송 실패: {e}")

        accumulated_text_parts: List[str] = []
        final_result_text: str = ""
        last_activity = time.time()
        started_at = last_activity
        has_started_tokens = False

        # Watchdog timeouts:
        # - 45s to initialize and emit first token
        # - 35s maximum silence between token deltas once streaming starts
        INIT_TIMEOUT_SEC = 45.0
        STALL_TIMEOUT_SEC = 35.0

        try:
            while True:
                if self._cancel_requested:
                    proc.kill()
                    break

                try:
                    line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    idle_sec = time.time() - last_activity
                    limit = STALL_TIMEOUT_SEC if has_started_tokens else INIT_TIMEOUT_SEC
                    if idle_sec > limit:
                        proc.kill()
                        partial_so_far = "".join(accumulated_text_parts)
                        partial_dict = extract_json_from_llm_response(partial_so_far)
                        if partial_dict:
                            # Return partial progress so caller saves it and only retries remaining!
                            return partial_dict
                        raise TimeoutError(
                            f"AGY 응답 지연 타임아웃 ({int(idle_sec)}초 동안 새 토큰 수신 없음)"
                        )
                    continue

                if line is None:
                    break

                last_activity = time.time()
                line_str = line.strip()
                if not line_str.startswith("{"):
                    continue

                try:
                    ev = json.loads(line_str)
                except Exception:
                    continue

                ev_type = ev.get("event")
                cid_candidate = (
                    ev.get("conversation_id")
                    or ev.get("conversationId")
                    or (ev.get("init") or {}).get("conversation_id")
                    or (ev.get("init") or {}).get("conversationId")
                    or (ev.get("result") or {}).get("conversation_id")
                    or (ev.get("result") or {}).get("conversationId")
                )
                if cid_candidate and isinstance(cid_candidate, str):
                    with self._agy_track_lock:
                        self._tracked_agy_conversation_ids.add(cid_candidate.strip())

                if ev_type == "init":
                    if on_stream_delta:
                        on_stream_delta("status", f"AGY 세션 초기화 완료 ({selected_model}) - 번역 생성 시작...")
                elif ev_type == "step_update":
                    su = ev.get("step_update") or {}
                    step_type = su.get("step_type", "")
                    delta = su.get("text_delta", "")
                    if step_type == "agent_response" and delta:
                        has_started_tokens = True
                        accumulated_text_parts.append(delta)
                        if on_stream_delta:
                            on_stream_delta("delta", "".join(accumulated_text_parts))
                    elif step_type == "thinking":
                        if on_stream_delta:
                            on_stream_delta("status", "모델 내부 추론 중...")
                    elif step_type not in ("user_input", "agent_response", "thinking", ""):
                        # If AGY unexpectedly tries to call a tool, abort immediately
                        proc.kill()
                        break
                elif ev_type == "result":
                    res_obj = ev.get("result") or {}
                    if res_obj.get("status") == "ERROR":
                        err_detail = res_obj.get("error", "알 수 없는 AGY 오류")
                        raise RuntimeError(f"AGY 오류: {err_detail}")
                    final_result_text = res_obj.get("response", "")
                    break
        finally:
            with self._proc_lock:
                self._active_procs.discard(proc)
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        combined = final_result_text if final_result_text.strip() else "".join(accumulated_text_parts)
        parsed = extract_json_from_llm_response(combined)
        if not parsed and not self._cancel_requested:
            err_txt = "".join(stderr_chunks).strip()
            raise RuntimeError(
                f"AGY로부터 유효한 JSON을 수신하지 못했습니다 (수신 길이: {len(combined)}자). {err_txt[:200]}"
            )
        return parsed

    def _call_gemini(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        payload_json_str: str,
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        if not api_key:
            raise ValueError("Gemini API Key가 설정되지 않았습니다.")
        requested_model = (model or "gemini-2.5-flash").strip()

        # Public Gemini API model fallback chain if an internal/preview alias returns 404
        model_candidates = [requested_model]
        if requested_model.startswith("gemini-4-argon"):
            for fb in ("gemini-4-argon", "gemini-3.1-pro-preview", "gemini-3.1-pro", "gemini-2.5-pro"):
                if fb not in model_candidates:
                    model_candidates.append(fb)
        elif requested_model.startswith("gemini-3.1-pro"):
            for fb in ("gemini-3.1-pro-preview", "gemini-3.1-pro", "gemini-2.5-pro"):
                if fb not in model_candidates:
                    model_candidates.append(fb)
        elif requested_model == "gemini-3.8-flash":
            model_candidates.extend(["gemini-2.5-flash", "gemini-2.0-flash"])
        elif requested_model == "gemini-3.5-flash-lite":
            model_candidates.extend(["gemini-2.5-flash-lite", "gemini-2.0-flash-lite"])

        last_err: Optional[Exception] = None
        for current_model in model_candidates:
            for use_thinking_cfg in (True, False):
                gen_cfg: dict = {
                    "temperature": 0.2,
                    "maxOutputTokens": 65536,
                    "responseMimeType": "application/json",
                }
                if use_thinking_cfg and ("2.5" in current_model or "3." in current_model or "4-" in current_model):
                    if "flash" in current_model:
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
                    else:
                        # Pro / Argon models may require a small non-zero thinking budget or default thinking
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 128}
                elif not use_thinking_cfg:
                    pass
                else:
                    # Models like gemini-2.0-flash / gemini-1.5-flash do not use thinkingConfig
                    pass

                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{current_model}:streamGenerateContent?alt=sse&key={api_key}"
                )
                body = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": payload_json_str}],
                        }
                    ],
                    "generationConfig": gen_cfg,
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    method="POST",
                )
                accumulated: List[str] = []
                try:
                    if on_stream_delta:
                        on_stream_delta("status", f"Gemini API ({current_model}) SSE 스트리밍 연결 중...")
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        for raw_line in resp:
                            if self._cancel_requested:
                                break
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if not data_str or data_str == "[DONE]":
                                continue
                            try:
                                chunk_obj = json.loads(data_str)
                            except Exception:
                                continue
                            usage_meta = chunk_obj.get("usageMetadata")
                            if usage_meta and on_stream_delta:
                                p_tok = int(usage_meta.get("promptTokenCount") or 0)
                                c_tok = int(usage_meta.get("candidatesTokenCount") or 0) + int(
                                    usage_meta.get("thoughtsTokenCount") or 0
                                )
                                if p_tok > 0 or c_tok > 0:
                                    on_stream_delta(
                                        "usage",
                                        json.dumps({"input_tokens": p_tok, "output_tokens": c_tok}),
                                    )
                            candidates = chunk_obj.get("candidates") or []
                            if not candidates:
                                continue
                            parts = (candidates[0].get("content") or {}).get("parts") or []
                            delta_text = "".join(
                                p.get("text", "") for p in parts if not p.get("thought")
                            )
                            if delta_text:
                                accumulated.append(delta_text)
                                if on_stream_delta:
                                    on_stream_delta("delta", "".join(accumulated))

                    full_text = "".join(accumulated)
                    if on_stream_delta and full_text:
                        on_stream_delta("delta", full_text)
                    parsed = extract_json_from_llm_response(full_text)
                    if not parsed and not self._cancel_requested:
                        raise RuntimeError(
                            f"Gemini 스트리밍 응답에서 유효한 JSON을 추출하지 못했습니다 (수신 길이: {len(full_text)}자)."
                        )
                    return parsed
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="ignore")
                    last_err = RuntimeError(f"Gemini API HTTP {e.code} ({current_model}): {err_body[:300]}")
                    # If 400 due to thinkingConfig, retry same model without thinkingConfig
                    if e.code == 400 and "thinkingConfig" in gen_cfg and use_thinking_cfg:
                        continue
                    # If 404 model not found on public endpoint, try next fallback model
                    if e.code == 404 and current_model != model_candidates[-1]:
                        break
                    raise last_err
                except Exception as e:
                    if accumulated:
                        partial = extract_json_from_llm_response("".join(accumulated))
                        if partial:
                            return partial
                    raise
                break
        if last_err:
            raise last_err
        raise RuntimeError("Gemini API 호출 실패")

    def _call_openai(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        payload_json_str: str,
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        if not api_key:
            raise ValueError("OpenAI API Key가 설정되지 않았습니다.")
        model_name = model or "gpt-4o-mini"
        url = "https://api.openai.com/v1/chat/completions"
        body = {
            "model": model_name,
            "temperature": 0.2,
            "stream": True,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_json_str},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        accumulated: List[str] = []
        try:
            if on_stream_delta:
                on_stream_delta("status", f"OpenAI API ({model_name}) SSE 스트리밍 연결 중...")
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw_line in resp:
                    if self._cancel_requested:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_obj = json.loads(data_str)
                    except Exception:
                        continue
                    choices = chunk_obj.get("choices") or []
                    if not choices:
                        continue
                    delta_text = (choices[0].get("delta") or {}).get("content") or ""
                    if delta_text:
                        accumulated.append(delta_text)
                        if on_stream_delta:
                            on_stream_delta("delta", "".join(accumulated))

            full_text = "".join(accumulated)
            if on_stream_delta and full_text:
                on_stream_delta("delta", full_text)
            return extract_json_from_llm_response(full_text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenAI API HTTP {e.code}: {err_body[:300]}")
        except Exception:
            if accumulated:
                partial = extract_json_from_llm_response("".join(accumulated))
                if partial:
                    return partial
            raise

    def _call_claude(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        payload_json_str: str,
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        if not api_key:
            raise ValueError("Anthropic Claude API Key가 설정되지 않았습니다.")
        model_name = model or "claude-3-5-haiku-latest"
        url = "https://api.anthropic.com/v1/messages"
        body = {
            "model": model_name,
            "max_tokens": 8192,
            "temperature": 0.2,
            "stream": True,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": f"Translate the following JSON values to Korean and output ONLY raw JSON:\n{payload_json_str}",
                }
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        accumulated: List[str] = []
        try:
            if on_stream_delta:
                on_stream_delta("status", f"Claude API ({model_name}) SSE 스트리밍 연결 중...")
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw_line in resp:
                    if self._cancel_requested:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data_str)
                    except Exception:
                        continue
                    ev_type = ev.get("type", "")
                    if ev_type == "content_block_delta":
                        delta_text = (ev.get("delta") or {}).get("text") or ""
                        if delta_text:
                            accumulated.append(delta_text)
                            if on_stream_delta:
                                on_stream_delta("delta", "".join(accumulated))
                    elif ev_type == "error":
                        err_obj = ev.get("error") or {}
                        raise RuntimeError(f"Claude 스트리밍 오류: {err_obj.get('message', err_obj)}")

            full_text = "".join(accumulated)
            if on_stream_delta and full_text:
                on_stream_delta("delta", full_text)
            return extract_json_from_llm_response(full_text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Claude API HTTP {e.code}: {err_body[:300]}")
        except Exception:
            if accumulated:
                partial = extract_json_from_llm_response("".join(accumulated))
                if partial:
                    return partial
            raise

    def _call_ollama(
        self,
        model: str,
        ollama_url: str,
        system_prompt: str,
        payload_json_str: str,
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        base = (ollama_url or "http://localhost:11434").rstrip("/")
        model_name = model or "qwen2.5:14b"
        url = f"{base}/api/chat"
        body = {
            "model": model_name,
            "stream": True,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 8192},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_json_str},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        accumulated: List[str] = []
        try:
            if on_stream_delta:
                on_stream_delta("status", f"Ollama ({model_name}) 실시간 스트리밍 연결 중...")
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw_line in resp:
                    if self._cancel_requested:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    delta_text = (ev.get("message") or {}).get("content") or ""
                    if delta_text:
                        accumulated.append(delta_text)
                        if on_stream_delta:
                            on_stream_delta("delta", "".join(accumulated))
                    if ev.get("done"):
                        break

            full_text = "".join(accumulated)
            if on_stream_delta and full_text:
                on_stream_delta("delta", full_text)
            return extract_json_from_llm_response(full_text)
        except Exception as e:
            if accumulated:
                partial = extract_json_from_llm_response("".join(accumulated))
                if partial:
                    return partial
            raise RuntimeError(f"Ollama 연결 오류 ({url}): {e}")

    def _call_deepl(
        self,
        model: str,
        api_key: str,
        chunk_dict: Dict[str, str],
        on_stream_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, str]:
        if not api_key:
            raise ValueError("DeepL API Key가 설정되지 않았습니다.")
        is_free = api_key.strip().endswith(":fx") or model == "deepl-free"
        url = "https://api-free.deepl.com/v2/translate" if is_free else "https://api.deepl.com/v2/translate"

        keys = list(chunk_dict.keys())
        values = list(chunk_dict.values())

        if on_stream_delta:
            on_stream_delta("status", f"DeepL API ({len(keys)}문장) 배치 번역 중...")

        translated_map: Dict[str, str] = {}
        for i in range(0, len(keys), 50):
            if self._cancel_requested:
                break
            b_keys = keys[i : i + 50]
            b_vals = values[i : i + 50]
            body = {
                "text": b_vals,
                "source_lang": "EN",
                "target_lang": "KO",
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"DeepL-Auth-Key {api_key.strip()}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    translations = resp_data.get("translations", [])
                    for k, tr_obj in zip(b_keys, translations):
                        translated_map[k] = tr_obj.get("text", chunk_dict[k])
                    if on_stream_delta and translated_map:
                        on_stream_delta("delta", json.dumps(translated_map, ensure_ascii=False))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"DeepL API HTTP {e.code}: {err_body[:300]}")
        return translated_map

    @staticmethod
    def _build_adaptive_chunks(
        rep_key_to_en: Dict[str, str], max_keys_per_chunk: int, max_chars_per_chunk: int = 22000
    ) -> List[Dict[str, str]]:
        """
        Slice `rep_key_to_en` into chunks respecting BOTH `max_keys_per_chunk`
        AND `max_chars_per_chunk` (~22,000 chars) so long quest/guidebook chapters
        never create a 100KB monster request that stalls generation.
        """
        chunks: List[Dict[str, str]] = []
        current_chunk: Dict[str, str] = {}
        current_chars = 0

        for k, en_val in rep_key_to_en.items():
            item_chars = len(en_val) + 10
            if current_chunk and (
                len(current_chunk) >= max_keys_per_chunk
                or (current_chars + item_chars > max_chars_per_chunk)
            ):
                chunks.append(current_chunk)
                current_chunk = {}
                current_chars = 0
            current_chunk[k] = en_val
            current_chars += item_chars

        if current_chunk:
            chunks.append(current_chunk)
        return chunks

    def translate_entries(
        self,
        entry_ids: List[str],
        provider_id: str = "agy",
        model: str = "gemini-3.8-flash-low",
        api_key: str = "",
        ollama_url: str = "http://localhost:11434",
        chunk_size: int = 300,
        concurrency: int = 3,
        deduplicate: bool = True,
        use_tm_prematch: bool = True,
        translate_all: bool = False,
        pack_small_mods: bool = True,
        auto_clean_agy_sessions: bool = True,
        max_retries: int = 3,
        progress_callback: Optional[Callable[[dict], None]] = None,
        bilingual: bool = False,
    ) -> dict:
        """
        Translate selected ModEntries with:
        - Smart Cross-Mod Batching (`pack_small_mods`): bundles small mods into unified chunks up to `chunk_size`
        - Concurrent multi-worker chunk execution (`ThreadPoolExecutor`)
        - Real-time streaming progress, multi-worker monitor & live ETA calculation
        - Automatic watchdog timeout & up to `max_retries` retries with sub-chunk splitting
        - Incremental SQLite + Mod Archive saving per mod as soon as chunks arrive
        """
        self.reset_cancel()
        self.bilingual = bool(bilingual)
        if self.bilingual:
            # TM holds plain Korean translations; applying it would skip the English annotation.
            use_tm_prematch = False
        chunk_size = max(10, min(5000, int(chunk_size)))
        spec = PROVIDERS.get(provider_id)
        max_allowed_workers = spec.max_concurrency if spec else 8
        effective_concurrency = max(1, min(max_allowed_workers, int(concurrency or 1)))

        job_start_time = time.time()
        total_entries = len(entry_ids)

        init_cost = calculate_realtime_cost(provider_id, model, 0, 0, 0)
        summary = {
            "provider_id": provider_id,
            "model": model,
            "completed_entries": 0,
            "total_entries": total_entries,
            "translated_keys": 0,
            "tm_hits": 0,
            "dedup_hits": 0,
            "llm_calls": 0,
            "retries_count": 0,
            "warnings_count": 0,
            "concurrency": effective_concurrency,
            "pack_small_mods": pack_small_mods,
            "total_chunks_all": 0,
            "completed_chunks_all": 0,
            "total_sentences_all": 0,
            "done_sentences_all": 0,
            "completed_input_tokens": 0,
            "completed_output_tokens": 0,
            "completed_chars": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "tokens_per_sec": 0.0,
            "input_price_per_1m": init_cost["input_price_per_1m"],
            "output_price_per_1m": init_cost["output_price_per_1m"],
            "billed_input_cost_usd": 0.0,
            "billed_output_cost_usd": 0.0,
            "billed_total_cost_usd": 0.0,
            "billed_total_cost_krw": 0.0,
            "equiv_input_cost_usd": 0.0,
            "equiv_output_cost_usd": 0.0,
            "equiv_total_cost_usd": 0.0,
            "equiv_total_cost_krw": 0.0,
            "is_free_tier": init_cost["is_free_tier"],
            "elapsed_total_sec": 0.0,
            "elapsed_text": "0초",
            "eta_sec": 0.0,
            "eta_text": "계산 중...",
            "speed_sentences_per_sec": 0.0,
            "errors": [],
            "canceled": False,
        }

        # Live tracking across parallel workers
        active_worker_streams: Dict[str, dict] = {}
        saved_sentences_all = 0
        initial_estimated_total_sec = 10.0

        def build_aggregate_stream_and_update_eta(latest_worker_stream: Optional[dict] = None) -> dict:
            """Must be called while holding self._state_lock."""
            now = time.time()
            elapsed_total = max(0.1, now - job_start_time)
            in_flight_done = sum(w.get("in_flight_count", 0) for w in active_worker_streams.values())
            tot_sent = summary["total_sentences_all"]
            curr_done = min(tot_sent, saved_sentences_all + in_flight_done) if tot_sent > 0 else 0

            # Real-time Input / Output Token & Cost Aggregation across completed + in-flight workers
            in_flight_in_tok = sum(w.get("in_flight_input_tokens", 0) for w in active_worker_streams.values())
            in_flight_out_tok = sum(w.get("in_flight_output_tokens", 0) for w in active_worker_streams.values())
            in_flight_chars = sum(w.get("in_flight_chars", 0) for w in active_worker_streams.values())

            live_in_tok = summary["completed_input_tokens"] + in_flight_in_tok
            live_out_tok = summary["completed_output_tokens"] + in_flight_out_tok
            live_tot_tok = live_in_tok + live_out_tok
            live_chars = summary["completed_chars"] + in_flight_chars
            tok_per_sec = round(live_out_tok / elapsed_total, 1) if elapsed_total > 0.5 else 0.0

            rt_cost = calculate_realtime_cost(
                provider_id=provider_id,
                model=model,
                input_tokens=live_in_tok,
                output_tokens=live_out_tok,
                total_chars=live_chars,
            )
            summary["input_tokens"] = live_in_tok
            summary["output_tokens"] = live_out_tok
            summary["total_tokens"] = live_tot_tok
            summary["tokens_per_sec"] = tok_per_sec
            summary["input_price_per_1m"] = rt_cost["input_price_per_1m"]
            summary["output_price_per_1m"] = rt_cost["output_price_per_1m"]
            summary["billed_input_cost_usd"] = rt_cost["billed_input_cost_usd"]
            summary["billed_output_cost_usd"] = rt_cost["billed_output_cost_usd"]
            summary["billed_total_cost_usd"] = rt_cost["billed_total_cost_usd"]
            summary["billed_total_cost_krw"] = rt_cost["billed_total_cost_krw"]
            summary["equiv_input_cost_usd"] = rt_cost["equiv_input_cost_usd"]
            summary["equiv_output_cost_usd"] = rt_cost["equiv_output_cost_usd"]
            summary["equiv_total_cost_usd"] = rt_cost["equiv_total_cost_usd"]
            summary["equiv_total_cost_krw"] = rt_cost["equiv_total_cost_krw"]
            summary["is_free_tier"] = rt_cost["is_free_tier"]

            summary["done_sentences_all"] = curr_done
            summary["elapsed_total_sec"] = round(elapsed_total, 1)
            summary["elapsed_text"] = format_duration_ko(elapsed_total)
            speed = round(curr_done / elapsed_total, 1) if elapsed_total > 0.5 else 0.0
            summary["speed_sentences_per_sec"] = speed

            rem_sent = max(0, tot_sent - curr_done)
            if tot_sent <= 0 or rem_sent <= 0:
                eta_s = 0.0
                eta_txt = "완료"
            elif curr_done == 0:
                eta_s = max(2.0, initial_estimated_total_sec - elapsed_total)
                eta_txt = format_duration_ko(eta_s)
            else:
                measured_rate = curr_done / elapsed_total
                measured_eta = rem_sent / max(0.05, measured_rate)
                model_rem_eta = max(1.0, initial_estimated_total_sec * (rem_sent / max(1, tot_sent)))
                prog_ratio = curr_done / max(1, tot_sent)
                if prog_ratio < 0.15 and curr_done < 25:
                    eta_s = measured_eta * 0.5 + model_rem_eta * 0.5
                else:
                    eta_s = measured_eta * 0.85 + model_rem_eta * 0.15
                eta_txt = format_duration_ko(eta_s)

            summary["eta_sec"] = round(eta_s, 1)
            summary["eta_text"] = eta_txt

            workers_list = list(active_worker_streams.values())
            primary = latest_worker_stream or (workers_list[0] if workers_list else {})
            total_chars_streaming = sum(w.get("chars_received", 0) for w in workers_list)
            best_tail = primary.get("tail_preview", "")
            if not best_tail:
                for w in workers_list:
                    if w.get("tail_preview"):
                        best_tail = w["tail_preview"]
                        break

            return {
                "mod_name": primary.get("mod_name", "번역 진행 중"),
                "chunk_label": primary.get("chunk_label", f"병렬 워커 {len(workers_list)}개 가동 중"),
                "status_text": primary.get("status_text", ""),
                "done_count": curr_done,
                "total_count": max(1, tot_sent),
                "elapsed_sec": round(elapsed_total, 1),
                "chars_received": total_chars_streaming or primary.get("chars_received", 0),
                "input_tokens": live_in_tok,
                "output_tokens": live_out_tok,
                "total_tokens": live_tot_tok,
                "tokens_per_sec": tok_per_sec,
                "billed_total_cost_usd": rt_cost["billed_total_cost_usd"],
                "billed_total_cost_krw": rt_cost["billed_total_cost_krw"],
                "equiv_total_cost_usd": rt_cost["equiv_total_cost_usd"],
                "equiv_total_cost_krw": rt_cost["equiv_total_cost_krw"],
                "is_free_tier": rt_cost["is_free_tier"],
                "tail_preview": best_tail,
                "active_workers": workers_list,
                "active_workers_count": len(workers_list),
                "eta_text": eta_txt,
                "elapsed_text": summary["elapsed_text"],
                "speed_sentences_per_sec": speed,
            }

        def emit(event_type: str, msg: str = "", extra: Optional[dict] = None):
            if progress_callback:
                with self._state_lock:
                    agg_stream = build_aggregate_stream_and_update_eta(
                        extra.get("worker_stream") if extra else None
                    )
                    payload = {
                        "type": event_type,
                        "message": msg,
                        "summary": dict(summary),
                        "stream": agg_stream,
                    }
                    if extra:
                        for k, v in extra.items():
                            if k != "worker_stream":
                                payload[k] = v
                progress_callback(payload)

        pack_mode_txt = "소형 모드 묶음 배칭 ON" if pack_small_mods else "모드별 개별 요청"
        emit(
            "start",
            f"총 {total_entries}개 항목 번역을 시작합니다 "
            f"(엔진: {provider_id}/{model}, 동시 요청: {effective_concurrency}개 병렬, 최대 청크: {chunk_size}키, {pack_mode_txt}, 재시도: 최대 {max_retries}회)",
        )

        alpha_re = re.compile(r"[a-zA-Z]{2,}")

        # Phase 1: Pre-process entries (Passthrough + Global TM pre-match + Deduplication + Smart Multi-Mod Batching)
        chunk_tasks: List[dict] = []
        entry_remaining_chunks: Dict[str, int] = {}
        entry_index_map: Dict[str, int] = {}
        unpacked_chunk_estimate = 0

        # Rolling buffer for packing multiple small mods / remainder chunks into unified chunks up to `chunk_size`
        buf_chunk_dict: Dict[str, str] = {}
        buf_rep_to_targets: Dict[str, List[Tuple[str, str]]] = {}
        buf_en_to_rep: Dict[str, str] = {}
        buf_entries_map: Dict[str, ModEntry] = {}
        buf_chars: int = 0
        max_chars_per_chunk: int = 22000

        def flush_chunk_buffer():
            nonlocal buf_chunk_dict, buf_rep_to_targets, buf_en_to_rep, buf_entries_map, buf_chars
            if not buf_chunk_dict:
                return
            involved_entries = list(buf_entries_map.values())
            for inv_e in involved_entries:
                entry_remaining_chunks[inv_e.entry_id] = entry_remaining_chunks.get(inv_e.entry_id, 0) + 1

            if len(involved_entries) == 1:
                disp_name = involved_entries[0].display_name
            elif len(involved_entries) <= 3:
                disp_name = " + ".join(e.display_name for e in involved_entries)
            else:
                disp_name = f"📦 소형 모드 묶음 ({involved_entries[0].display_name} 외 {len(involved_entries) - 1}개)"

            mod_id_csv = ",".join(dict.fromkeys(e.mod_id for e in involved_entries))
            c_idx_global = len(chunk_tasks) + 1
            chunk_tasks.append({
                "task_id": f"batch#c{c_idx_global}",
                "involved_entries": involved_entries,
                "display_name": disp_name,
                "mod_id_csv": mod_id_csv,
                "c_idx": c_idx_global,
                "num_chunks": 0,  # Populated once all chunks are built
                "chunk_dict": dict(buf_chunk_dict),
                "rep_key_to_targets": dict(buf_rep_to_targets),
            })
            buf_chunk_dict = {}
            buf_rep_to_targets = {}
            buf_en_to_rep = {}
            buf_entries_map = {}
            buf_chars = 0

        for idx, entry_id in enumerate(entry_ids, 1):
            if self._cancel_requested:
                summary["canceled"] = True
                emit("canceled", "사용자 요청으로 번역이 즉시 중단되었습니다.")
                break

            entry = self.scanner.entries.get(entry_id)
            if not entry:
                continue

            entry_index_map[entry_id] = idx
            en_flat = self.scanner.get_en_dict(entry_id)
            builtin_ko = self.scanner.get_builtin_ko_dict(entry_id)
            cached_ko_with_en = dict(self.scanner.get_linked_cache_dict(entry_id))
            cached_ko_with_en.update(self.cache.get_entry_translations_with_en(entry_id))
            mod_en_to_ko = {
                s_en: s_ko
                for s_en, s_ko in cached_ko_with_en.values()
                if s_en and s_ko and s_ko.strip()
            }

            pending_keys: Dict[str, str] = {}
            passthrough_batch: List[Tuple[str, str, str]] = []
            archive_renamed_batch: List[Tuple[str, str, str]] = []

            for k, en_val in en_flat.items():
                if not translate_all:
                    if k in builtin_ko and builtin_ko[k].strip():
                        continue
                    if k in cached_ko_with_en:
                        saved_en, saved_ko = cached_ko_with_en[k]
                        # If the key exists in our Mod Archive and its English text is unchanged, skip!
                        if saved_ko and saved_ko.strip() and (
                            saved_en == en_val or (not saved_en and entry.source_type != "ftbquests")
                        ):
                            continue
                    # If the key was renamed in a new version of this mod (or English text matches another key in this mod)
                    if en_val in mod_en_to_ko:
                        archive_renamed_batch.append((k, en_val, mod_en_to_ko[en_val]))
                        with self._state_lock:
                            summary["tm_hits"] += 1
                            summary["translated_keys"] += 1
                        continue
                stripped_tags = re.sub(r"\{(?:@[a-zA-Z0-9_]+|image:[^}]+)\}", "", en_val)
                if not alpha_re.search(stripped_tags):
                    passthrough_batch.append((k, en_val, en_val))
                    with self._state_lock:
                        summary["translated_keys"] += 1
                else:
                    pending_keys[k] = en_val

            if archive_renamed_batch:
                self.cache.save_translations_batch(entry_id, archive_renamed_batch, provider="mod_archive_reuse")
                emit(
                    "tm_hit",
                    f"[{entry.display_name}] 모드 보관소에서 버전 호환 문장 {len(archive_renamed_batch)}개 자동 재사용 ($0)",
                )

            if passthrough_batch:
                self.cache.save_translations_batch(entry_id, passthrough_batch, provider="passthrough")

            if not pending_keys:
                with self._state_lock:
                    summary["completed_entries"] += 1
                self._refresh_entry_stats(entry_id)
                emit("entry_skip", f"[{idx}/{total_entries}] {entry.display_name}: 이미 모든 키가 번역되어 있습니다.")
                continue

            # Step 1: Global Translation Memory pre-match
            if use_tm_prematch and not translate_all:
                tm_matches = self.cache.lookup_tm_batch(list(pending_keys.values()))
                tm_batch_to_save: List[Tuple[str, str, str]] = []
                remaining_after_tm: Dict[str, str] = {}
                for k, en_val in pending_keys.items():
                    if en_val in tm_matches:
                        ko_val = tm_matches[en_val]
                        tm_batch_to_save.append((k, en_val, ko_val))
                        with self._state_lock:
                            summary["tm_hits"] += 1
                            summary["translated_keys"] += 1
                    else:
                        remaining_after_tm[k] = en_val

                if tm_batch_to_save:
                    self.cache.save_translations_batch(entry_id, tm_batch_to_save, provider="tm_exact")
                    emit(
                        "tm_hit",
                        f"[{entry.display_name}] 번역 메모리(TM)에서 중복 문장 {len(tm_batch_to_save)}개 즉시 적용 ($0)",
                    )
                pending_keys = remaining_after_tm

            if not pending_keys:
                with self._state_lock:
                    summary["completed_entries"] += 1
                self._refresh_entry_stats(entry_id)
                emit("entry_done", f"[{idx}/{total_entries}] {entry.display_name} 완료 (전량 TM 캐시 히트!)")
                continue

            # Count how many chunks this mod would have taken if NOT packed
            unique_in_mod = len(set(pending_keys.values())) if deduplicate else len(pending_keys)
            unpacked_chunk_estimate += max(1, math.ceil(unique_in_mod / chunk_size))

            # If pack_small_mods is disabled, flush any previous entry before starting this one
            if not pack_small_mods and buf_chunk_dict:
                flush_chunk_buffer()

            # Feed pending keys of this mod into the adaptive chunk buffer
            for k, en_val in pending_keys.items():
                if deduplicate and en_val in buf_en_to_rep:
                    # Identical English sentence already present in this current chunk buffer!
                    existing_rep = buf_en_to_rep[en_val]
                    buf_rep_to_targets[existing_rep].append((entry_id, k))
                    buf_entries_map[entry_id] = entry
                    continue

                item_chars = len(en_val) + 10
                if buf_chunk_dict and (
                    len(buf_chunk_dict) >= chunk_size
                    or (buf_chars + item_chars > max_chars_per_chunk)
                ):
                    flush_chunk_buffer()

                rep_k = f"{entry_id}::{k}" if pack_small_mods else k
                buf_chunk_dict[rep_k] = en_val
                buf_rep_to_targets[rep_k] = [(entry_id, k)]
                if deduplicate:
                    buf_en_to_rep[en_val] = rep_k
                buf_entries_map[entry_id] = entry
                buf_chars += item_chars

            if not pack_small_mods and buf_chunk_dict:
                flush_chunk_buffer()

        if buf_chunk_dict:
            flush_chunk_buffer()

        # Populate total num_chunks on all tasks
        total_chunks_all = len(chunk_tasks)
        for t in chunk_tasks:
            t["num_chunks"] = total_chunks_all
        saved_reqs = max(0, unpacked_chunk_estimate - total_chunks_all)

        # Compute initial estimated duration & totals across all queued chunks
        total_sentences_all = sum(len(t["chunk_dict"]) for t in chunk_tasks)
        total_chars_all = sum(sum(len(v) for v in t["chunk_dict"].values()) for t in chunk_tasks)
        est_out_tokens = int((total_chars_all / 3.5) * 1.18) + (total_sentences_all * 5)

        if spec and total_chunks_all > 0:
            active_w = min(effective_concurrency, total_chunks_all)
            serial_s = (total_chunks_all * spec.init_latency_sec) + (
                est_out_tokens / max(1.0, spec.output_tokens_per_sec)
            )
            waves = math.ceil(total_chunks_all / active_w)
            initial_estimated_total_sec = (serial_s / active_w) * 0.75 + (waves * (serial_s / total_chunks_all)) * 0.25
        else:
            initial_estimated_total_sec = max(5.0, total_sentences_all * 0.15 / max(1, effective_concurrency))

        with self._state_lock:
            summary["total_chunks_all"] = total_chunks_all
            summary["total_sentences_all"] = total_sentences_all

        if total_chunks_all > 0 and not self._cancel_requested:
            pack_saved_msg = (
                f" (소형 모드 묶음 배칭으로 {unpacked_chunk_estimate}회 → {total_chunks_all}회 요청으로 -{saved_reqs}회 병목 단축!)"
                if saved_reqs > 0
                else ""
            )
            emit(
                "queue_ready",
                f"⚡ 병렬 작업 큐 준비 완료: 총 {total_chunks_all}개 청크 ({total_sentences_all:,}개 고유 문장){pack_saved_msg} | "
                f"동시 요청 {min(effective_concurrency, total_chunks_all)}개 워커 가동 | "
                f"예상 소요 시간: {format_duration_ko(initial_estimated_total_sec)}",
            )

            def run_single_task(task: dict):
                nonlocal saved_sentences_all
                if self._cancel_requested:
                    return

                involved_entries: List[ModEntry] = task["involved_entries"]
                display_name: str = task["display_name"]
                mod_id_csv: str = task["mod_id_csv"]
                c_idx: int = task["c_idx"]
                num_chunks: int = task["num_chunks"]
                task_id: str = task["task_id"]

                done_in_task = self._process_chunk_with_retries(
                    task_id=task_id,
                    display_name=display_name,
                    mod_id_csv=mod_id_csv,
                    c_idx=c_idx,
                    num_chunks=num_chunks,
                    chunk_dict=task["chunk_dict"],
                    rep_key_to_targets=task["rep_key_to_targets"],
                    provider_id=provider_id,
                    model=model,
                    api_key=api_key,
                    ollama_url=ollama_url,
                    use_tm_prematch=use_tm_prematch and not translate_all,
                    max_retries=max_retries,
                    summary=summary,
                    active_worker_streams=active_worker_streams,
                    emit=emit,
                )

                finished_entries_now: List[ModEntry] = []
                with self._state_lock:
                    saved_sentences_all += done_in_task
                    summary["completed_chunks_all"] += 1
                    active_worker_streams.pop(task_id, None)
                    for inv_e in involved_entries:
                        rem_c = entry_remaining_chunks.get(inv_e.entry_id, 1) - 1
                        entry_remaining_chunks[inv_e.entry_id] = rem_c
                        if rem_c <= 0:
                            summary["completed_entries"] += 1
                            finished_entries_now.append(inv_e)

                if finished_entries_now and not self._cancel_requested:
                    for fin_e in finished_entries_now:
                        self._refresh_entry_stats(fin_e.entry_id)
                    if len(finished_entries_now) <= 2:
                        for fin_e in finished_entries_now:
                            e_idx = entry_index_map.get(fin_e.entry_id, 0)
                            emit(
                                "entry_done",
                                f"[{e_idx}/{total_entries}] {fin_e.display_name} ({fin_e.mod_id}) 번역 완료!",
                            )
                    else:
                        names_preview = ", ".join(e.display_name for e in finished_entries_now[:3])
                        more_cnt = len(finished_entries_now) - 3
                        more_txt = f" 외 {more_cnt}개" if more_cnt > 0 else ""
                        emit(
                            "entry_done",
                            f"📦 묶음 청크 완료로 소형 모드 {len(finished_entries_now)}개 동시 완료! ({names_preview}{more_txt})",
                        )

            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = [executor.submit(run_single_task, t) for t in chunk_tasks]
                for fut in as_completed(futures):
                    if self._cancel_requested:
                        summary["canceled"] = True
                        break
                    try:
                        fut.result()
                    except Exception as exc:
                        with self._state_lock:
                            summary["errors"].append(str(exc))

        if self._cancel_requested:
            summary["canceled"] = True
            emit("canceled", "사용자 요청으로 번역이 즉시 중단되었습니다.")

        if provider_id == "agy" and auto_clean_agy_sessions:
            try:
                clean_res = self.cleanup_agy_sessions()
                cleaned_cnt = clean_res.get("cleaned_sessions_count", 0)
                summary["cleaned_agy_sessions"] = cleaned_cnt
                if cleaned_cnt > 0:
                    emit(
                        "session_cleanup",
                        f"🧹 AGY 임시 번역 세션 {cleaned_cnt:,}개 자동 정리 완료 (내 계정 대화 내역 보호됨)",
                    )
            except Exception as e:
                emit("chunk_error", f"  [WARN] AGY 임시 세션 정리 중 오류: {e}")

        total_elapsed = time.time() - job_start_time
        cost_txt = (
            "비용: 무료 ($0)"
            if summary["is_free_tier"]
            else f"비용: ${summary['equiv_total_cost_usd']:.4f} (약 {int(round(summary['equiv_total_cost_krw'])):,}원)"
        )
        emit(
            "finished",
            f"번역 작업 종료 (소요 시간: {format_duration_ko(total_elapsed)}): "
            f"총 {summary['translated_keys']:,}개 키 완료 | "
            f"실시간 토큰: 입력 {summary['input_tokens']:,} + 출력 {summary['output_tokens']:,} = 총 {summary['total_tokens']:,} tok | "
            f"{cost_txt} "
            f"(TM 히트: {summary['tm_hits']:,}, 중복 절약: {summary['dedup_hits']:,}, 재시도: {summary['retries_count']}회)",
        )
        return summary

    def _process_chunk_with_retries(
        self,
        task_id: str,
        display_name: str,
        mod_id_csv: str,
        c_idx: int,
        num_chunks: int,
        chunk_dict: Dict[str, str],
        rep_key_to_targets: Dict[str, List[Tuple[str, str]]],
        provider_id: str,
        model: str,
        api_key: str,
        ollama_url: str,
        use_tm_prematch: bool,
        max_retries: int,
        summary: dict,
        active_worker_streams: Dict[str, dict],
        emit: Callable[[str, str, Optional[dict]], None],
    ) -> int:
        """
        Execute a chunk (single-mod or multi-mod bundled) inside a worker thread with real-time
        streaming feedback and up to `max_retries` retries.
        Returns the number of unique sentences completed in this chunk.
        """
        remaining_dict = dict(chunk_dict)
        total_in_chunk = len(chunk_dict)
        saved_in_chunk = 0

        # Cross-worker live TM check: if another parallel worker just translated identical sentences, reuse them!
        if use_tm_prematch and remaining_dict:
            live_tm = self.cache.lookup_tm_batch(list(remaining_dict.values()))
            if live_tm:
                per_entry_tm: Dict[str, List[Tuple[str, str, str]]] = {}
                matched_rep_keys: List[str] = []
                total_tm_keys_saved = 0
                for orig_k, en_val in remaining_dict.items():
                    if en_val in live_tm:
                        ko_val = live_tm[en_val]
                        for target_eid, target_k in rep_key_to_targets[orig_k]:
                            per_entry_tm.setdefault(target_eid, []).append((target_k, en_val, ko_val))
                            total_tm_keys_saved += 1
                        matched_rep_keys.append(orig_k)
                if per_entry_tm:
                    for target_eid, tm_batch in per_entry_tm.items():
                        self.cache.save_translations_batch(target_eid, tm_batch, provider="tm_live_parallel")
                    with self._state_lock:
                        summary["tm_hits"] += total_tm_keys_saved
                        summary["translated_keys"] += total_tm_keys_saved
                    for ok in matched_rep_keys:
                        remaining_dict.pop(ok, None)
                    saved_in_chunk += len(matched_rep_keys)

        if not remaining_dict:
            return saved_in_chunk

        for attempt in range(1, max_retries + 2):
            if self._cancel_requested or not remaining_dict:
                break

            is_retry = attempt > 1
            if is_retry:
                with self._state_lock:
                    summary["retries_count"] += 1
                emit(
                    "chunk_retry",
                    f"  [재시도 {attempt - 1}/{max_retries}] [{display_name}] 청크 {c_idx}/{num_chunks} "
                    f"미완료 {len(remaining_dict)}문장 재요청 중...",
                )
            else:
                total_chars = sum(len(v) for v in remaining_dict.values())
                worker_info = {
                    "task_id": task_id,
                    "mod_name": display_name,
                    "chunk_label": f"청크 {c_idx}/{num_chunks}",
                    "status_text": "요청 시작...",
                    "done_count": saved_in_chunk,
                    "total_count": total_in_chunk,
                    "in_flight_count": saved_in_chunk,
                    "in_flight_input_tokens": 0,
                    "in_flight_output_tokens": 0,
                    "in_flight_chars": 0,
                    "worker_input_tokens": 0,
                    "worker_output_tokens": 0,
                    "elapsed_sec": 0.0,
                    "chars_received": 0,
                    "tail_preview": "",
                }
                with self._state_lock:
                    active_worker_streams[task_id] = worker_info
                emit(
                    "chunk_start",
                    f"  -> [{display_name}] 청크 {c_idx}/{num_chunks} 요청 시작 "
                    f"({len(remaining_dict)}문장, {total_chars:,}자)...",
                    {"worker_stream": worker_info},
                )

            # If retrying a large batch (> 120 items), split it into smaller sub-batches
            sub_batches: List[Dict[str, str]] = []
            if is_retry and len(remaining_dict) > 120:
                sub_size = max(50, len(remaining_dict) // 2)
                rem_keys = list(remaining_dict.keys())
                for i in range(0, len(rem_keys), sub_size):
                    sk = rem_keys[i : i + sub_size]
                    sub_batches.append({k: remaining_dict[k] for k in sk})
            else:
                sub_batches = [dict(remaining_dict)]

            attempt_had_error = False

            for sub_idx, sub_dict in enumerate(sub_batches, 1):
                if self._cancel_requested:
                    break

                orig_keys = list(sub_dict.keys())
                idx_to_orig_key = {str(i): k for i, k in enumerate(orig_keys)}
                compact_input = {str(i): sub_dict[k] for i, k in enumerate(orig_keys)}

                # Estimate request input tokens immediately when sub-batch starts
                sub_sys_prompt = self.build_system_prompt(mod_id_csv, list(compact_input.values()))
                sub_payload_str = json.dumps(compact_input, ensure_ascii=False, indent=2)
                sub_in_tok = estimate_text_tokens(sub_sys_prompt) + estimate_text_tokens(sub_payload_str)
                sub_in_chars = sum(len(v) for v in compact_input.values())

                t0 = time.time()
                last_ui_push = 0.0
                last_tail_preview = ""
                last_chars_len = 0
                last_stream_content = ""
                api_reported_in_tok = 0
                api_reported_out_tok = 0

                def on_stream_delta(kind: str, content: str):
                    nonlocal last_ui_push, last_tail_preview, last_chars_len, last_stream_content
                    nonlocal api_reported_in_tok, api_reported_out_tok
                    now = time.time()
                    if kind == "usage":
                        try:
                            u_obj = json.loads(content)
                            if int(u_obj.get("input_tokens") or 0) > 0:
                                api_reported_in_tok = int(u_obj["input_tokens"])
                            if int(u_obj.get("output_tokens") or 0) > 0:
                                api_reported_out_tok = int(u_obj["output_tokens"])
                        except Exception:
                            pass
                        return

                    cur_in_tok = api_reported_in_tok if api_reported_in_tok > 0 else sub_in_tok
                    if kind == "status":
                        cur_out_tok = max(api_reported_out_tok, estimate_text_tokens(last_stream_content))
                        w_state = {
                            "task_id": task_id,
                            "mod_name": display_name,
                            "chunk_label": f"청크 {c_idx}/{num_chunks}" + (f" (재시도 {attempt-1})" if is_retry else ""),
                            "status_text": content,
                            "done_count": saved_in_chunk,
                            "total_count": total_in_chunk,
                            "in_flight_count": saved_in_chunk,
                            "in_flight_input_tokens": cur_in_tok,
                            "in_flight_output_tokens": cur_out_tok,
                            "in_flight_chars": sub_in_chars,
                            "worker_input_tokens": cur_in_tok,
                            "worker_output_tokens": cur_out_tok,
                            "elapsed_sec": round(now - t0, 1),
                            "chars_received": last_chars_len,
                            "tail_preview": last_tail_preview,
                        }
                        with self._state_lock:
                            active_worker_streams[task_id] = w_state
                        emit("stream_update", "", {"worker_stream": w_state})
                        return

                    last_stream_content = content
                    last_chars_len = len(content)
                    last_tail_preview = content.strip()[-180:].replace("\n", " ")

                    # Throttle UI stream parsing per worker to once every 0.18s for smooth real-time SSE updates
                    if now - last_ui_push < 0.18:
                        return
                    last_ui_push = now

                    cur_out_tok = max(api_reported_out_tok, estimate_text_tokens(content))
                    partial_parsed = extract_json_from_llm_response(content)
                    streamed_count = len(partial_parsed)
                    live_samples: List[Dict[str, str]] = []
                    for idx_str in list(partial_parsed.keys())[-4:]:
                        if idx_str in idx_to_orig_key:
                            ok = idx_to_orig_key[idx_str]
                            disp_k = ok.split("::", 1)[-1] if "::" in ok else ok
                            live_samples.append({
                                "key": disp_k,
                                "en": sub_dict[ok],
                                "ko": partial_parsed[idx_str],
                            })

                    w_state = {
                        "task_id": task_id,
                        "mod_name": display_name,
                        "chunk_label": f"청크 {c_idx}/{num_chunks}" + (f" (재시도 {attempt-1})" if is_retry else ""),
                        "status_text": f"스트리밍 수신 중 ({saved_in_chunk + streamed_count}/{total_in_chunk} 문장 · In {cur_in_tok:,} / Out {cur_out_tok:,} tok)",
                        "done_count": saved_in_chunk + streamed_count,
                        "total_count": total_in_chunk,
                        "in_flight_count": saved_in_chunk + streamed_count,
                        "in_flight_input_tokens": cur_in_tok,
                        "in_flight_output_tokens": cur_out_tok,
                        "in_flight_chars": sub_in_chars,
                        "worker_input_tokens": cur_in_tok,
                        "worker_output_tokens": cur_out_tok,
                        "elapsed_sec": round(now - t0, 1),
                        "chars_received": last_chars_len,
                        "tail_preview": last_tail_preview,
                    }
                    with self._state_lock:
                        active_worker_streams[task_id] = w_state
                    emit(
                        "stream_update",
                        "",
                        {
                            "samples": live_samples,
                            "worker_stream": w_state,
                        },
                    )

                try:
                    with self._state_lock:
                        summary["llm_calls"] += 1
                    compact_result = self.call_provider(
                        provider_id=provider_id,
                        model=model,
                        api_key=api_key,
                        mod_id=mod_id_csv,
                        chunk_dict=compact_input,
                        ollama_url=ollama_url,
                        on_stream_delta=on_stream_delta,
                    )
                    elapsed = time.time() - t0

                    final_in_tok = api_reported_in_tok if api_reported_in_tok > 0 else sub_in_tok
                    final_out_tok = max(
                        api_reported_out_tok,
                        estimate_text_tokens(
                            last_stream_content
                            if last_stream_content
                            else json.dumps(compact_result, ensure_ascii=False, indent=2)
                        ),
                    )
                    chunk_cost = calculate_realtime_cost(
                        provider_id, model, final_in_tok, final_out_tok, sub_in_chars
                    )
                    chunk_cost_txt = (
                        "" if chunk_cost["is_free_tier"] else f" · ${chunk_cost['equiv_total_cost_usd']:.4f}"
                    )

                    # Map compact keys back to each target entry_id and save immediately
                    per_entry_batches: Dict[str, List[Tuple[str, str, str]]] = {}
                    sample_previews: List[Dict[str, str]] = []
                    newly_completed_rep_keys: List[str] = []
                    warns_added = 0
                    dedup_added = 0
                    total_keys_saved_in_sub = 0

                    for idx_str, orig_k in idx_to_orig_key.items():
                        raw_ko = compact_result.get(idx_str) or compact_result.get(orig_k) or ""
                        if not raw_ko or not str(raw_ko).strip():
                            continue
                        en_val = sub_dict[orig_k]
                        repaired_ko, warns = validate_and_repair_translation(en_val, str(raw_ko))
                        if warns:
                            warns_added += len(warns)

                        all_targets = rep_key_to_targets[orig_k]
                        if len(all_targets) > 1:
                            dedup_added += len(all_targets) - 1

                        for target_eid, target_k in all_targets:
                            per_entry_batches.setdefault(target_eid, []).append((target_k, en_val, repaired_ko))
                            total_keys_saved_in_sub += 1

                        newly_completed_rep_keys.append(orig_k)
                        if len(sample_previews) < 6:
                            disp_k = orig_k.split("::", 1)[-1] if "::" in orig_k else orig_k
                            sample_previews.append({"key": disp_k, "en": en_val, "ko": repaired_ko})

                    for target_eid, batch_list in per_entry_batches.items():
                        self.cache.save_translations_batch(
                            target_eid, batch_list, provider=f"{provider_id}:{model}"
                        )

                    for ok in newly_completed_rep_keys:
                        remaining_dict.pop(ok, None)
                    saved_in_chunk += len(newly_completed_rep_keys)

                    w_done_state = {
                        "task_id": task_id,
                        "mod_name": display_name,
                        "chunk_label": f"청크 {c_idx}/{num_chunks} 완료",
                        "status_text": f"청크 저장 완료 ({saved_in_chunk}/{total_in_chunk} 문장 · In {final_in_tok:,} / Out {final_out_tok:,} tok)",
                        "done_count": saved_in_chunk,
                        "total_count": total_in_chunk,
                        "in_flight_count": saved_in_chunk,
                        "in_flight_input_tokens": 0,
                        "in_flight_output_tokens": 0,
                        "in_flight_chars": 0,
                        "worker_input_tokens": final_in_tok,
                        "worker_output_tokens": final_out_tok,
                        "elapsed_sec": round(elapsed, 1),
                        "chars_received": last_chars_len,
                        "tail_preview": last_tail_preview,
                    }
                    with self._state_lock:
                        summary["completed_input_tokens"] += final_in_tok
                        summary["completed_output_tokens"] += final_out_tok
                        summary["completed_chars"] += sub_in_chars
                        summary["warnings_count"] += warns_added
                        summary["dedup_hits"] += dedup_added
                        summary["translated_keys"] += total_keys_saved_in_sub
                        active_worker_streams[task_id] = w_done_state

                    emit(
                        "chunk_done",
                        f"  [OK] [{display_name}] 청크 {c_idx}/{num_chunks} 수신 완료 "
                        f"({elapsed:.1f}초, {len(newly_completed_rep_keys)}/{len(sub_dict)}문장 -> {total_keys_saved_in_sub}키 저장 | "
                        f"In {final_in_tok:,} / Out {final_out_tok:,} tok{chunk_cost_txt})",
                        {
                            "samples": sample_previews,
                            "worker_stream": w_done_state,
                        },
                    )
                except Exception as e:
                    attempt_had_error = True
                    # Clear in-flight tokens for this failed attempt so they aren't double-counted
                    with self._state_lock:
                        if task_id in active_worker_streams:
                            active_worker_streams[task_id]["in_flight_input_tokens"] = 0
                            active_worker_streams[task_id]["in_flight_output_tokens"] = 0
                            active_worker_streams[task_id]["in_flight_chars"] = 0
                    err_msg = f"[{display_name}] 청크 {c_idx}/{num_chunks} (시도 {attempt}/{max_retries + 1}) 오류: {e}"
                    emit("chunk_error", f"  [WARN] {err_msg}")
                    if attempt <= max_retries and not self._cancel_requested:
                        time.sleep(1.5)

            if not remaining_dict:
                break
            if not attempt_had_error and remaining_dict:
                continue

        if remaining_dict and not self._cancel_requested:
            fail_msg = f"[{display_name}] 청크 {c_idx}/{num_chunks}: {max_retries}회 재시도 후에도 {len(remaining_dict)}문장 미번역"
            with self._state_lock:
                summary["errors"].append(fail_msg)
            emit("chunk_error", f"  [WARN] {fail_msg}")

        return saved_in_chunk

    def _refresh_entry_stats(self, entry_id: str) -> None:
        """Refresh a single ModEntry's cached/untranslated counts in memory after translation."""
        with self._state_lock:
            if entry_id not in self.scanner.entries:
                return
            old = self.scanner.entries[entry_id]
            en_flat = self.scanner.get_en_dict(entry_id)
            ko_flat = self.scanner.get_builtin_ko_dict(entry_id)
            cached_map = dict(self.scanner.get_linked_cache_dict(entry_id))
            cached_map.update(self.cache.get_entry_translations_with_en(entry_id))
            self.scanner._register_entry(
                entry_id=old.entry_id,
                mod_id=old.mod_id,
                display_name=old.display_name,
                version=old.version,
                source_type=old.source_type,
                source_file=old.source_file,
                en_path_in_source=old.en_path_in_source,
                en_flat=en_flat,
                ko_flat=ko_flat,
                cached_map=cached_map,
            )

    @staticmethod
    def _get_agy_app_data_dir() -> str:
        """Return the Antigravity CLI appDataDir (`~/.gemini/antigravity-cli`)."""
        return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")

    @staticmethod
    def _is_translation_session_row(
        cid: str,
        title: str,
        preview: str,
        workspace_uris: str,
        step_count: int,
        tracked_ids: Set[str],
    ) -> bool:
        """
        Determine whether a conversation summary row in `conversation_summaries.db` is a
        one-off automated translation session created by `mc_translator`.
        Strictly protects normal user conversations and any long interactive sessions.
        """
        if not cid:
            return False
        # Never touch interactive sessions with > 12 steps unless explicitly tracked by our Popen call
        if cid in tracked_ids:
            return True
        if step_count and int(step_count) > 12:
            return False

        ws = (workspace_uris or "").lower()
        prev = preview or ""
        ttl = title or ""

        if "agy_sandbox" in ws:
            return True
        if "expert Korean translator for Minecraft" in prev:
            return True
        if "[INPUT JSON TO TRANSLATE]" in prev:
            return True
        if "Translate the values of the provided JSON" in prev:
            return True
        if ttl in (
            "Minecraft Modpack Translation",
            "JSON Translation to Korean",
            "Korean Translation of JSON Values",
            "Nautec Modpack Korean Translation",
        ) and int(step_count or 0) <= 6:
            return True
        if prev.startswith("Translate ") and "JSON" in prev and "Korean" in prev and int(step_count or 0) <= 6:
            return True
        return False

    @staticmethod
    def _decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
        val = 0
        shift = 0
        n = len(buf)
        while pos < n:
            b = buf[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                return val, pos
            shift += 7
        return val, pos

    @classmethod
    def _filter_jetbox_summaries_pb(cls, pb_path: str, remove_cids: Set[str]) -> int:
        """
        Strip removed conversation IDs from `jetbox_summaries_proto.pb` while keeping all
        remaining binary protobuf frames 100% intact.
        """
        if not remove_cids or not os.path.isfile(pb_path):
            return 0
        try:
            with open(pb_path, "rb") as f:
                raw_pb = f.read()
        except Exception:
            return 0

        if not raw_pb:
            return 0

        kept_frames: List[bytes] = []
        removed_count = 0
        pos = 0
        n = len(raw_pb)

        try:
            while pos < n:
                frame_start = pos
                tag, pos2 = cls._decode_varint(raw_pb, pos)
                wire_type = tag & 7
                if wire_type == 2:
                    length, pos3 = cls._decode_varint(raw_pb, pos2)
                    frame_end = pos3 + length
                    if frame_end > n:
                        # Malformed tail, abort modification safely
                        return 0
                    chunk = raw_pb[pos3:frame_end]
                    # Check subfield 1 (conversation_id string)
                    cid_str = ""
                    if chunk:
                        stag, spos = cls._decode_varint(chunk, 0)
                        if (stag >> 3) == 1 and (stag & 7) == 2:
                            slen, spos2 = cls._decode_varint(chunk, spos)
                            if spos2 + slen <= len(chunk):
                                cid_str = chunk[spos2 : spos2 + slen].decode("utf-8", errors="ignore")
                    if cid_str and (
                        cid_str in remove_cids
                        or b"expert Korean translator for Minecraft" in chunk
                        or b"agy_sandbox" in chunk
                    ):
                        removed_count += 1
                    else:
                        kept_frames.append(raw_pb[frame_start:frame_end])
                    pos = frame_end
                elif wire_type == 0:
                    _, pos = cls._decode_varint(raw_pb, pos2)
                    kept_frames.append(raw_pb[frame_start:pos])
                else:
                    # Unexpected wire type: do not corrupt file
                    return 0
        except Exception:
            return 0

        if removed_count > 0:
            try:
                tmp_path = pb_path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(b"".join(kept_frames))
                os.replace(tmp_path, pb_path)
            except Exception:
                pass
        return removed_count

    def get_agy_translation_sessions_stats(self) -> dict:
        """Count how many temporary translation sessions currently exist in AGY CLI storage."""
        app_dir = self._get_agy_app_data_dir()
        summary_db = os.path.join(app_dir, "conversation_summaries.db")
        if not os.path.isfile(summary_db):
            return {"translation_sessions_count": 0, "total_sessions_count": 0}

        with self._agy_track_lock:
            tracked = set(self._tracked_agy_conversation_ids)

        trans_count = 0
        total_count = 0
        try:
            conn = sqlite3.connect(summary_db, timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT conversation_id, title, preview, workspace_uris, step_count FROM conversation_summaries"
                ).fetchall()
                total_count = len(rows)
                for cid, title, prev, ws, sc in rows:
                    if self._is_translation_session_row(cid, title, prev, ws, sc, tracked):
                        trans_count += 1
            finally:
                conn.close()
        except Exception:
            pass

        return {
            "translation_sessions_count": trans_count,
            "total_sessions_count": total_count,
        }

    def cleanup_agy_sessions(self) -> dict:
        """
        Delete temporary one-off translation sessions generated by `agy` CLI across:
        1. `conversation_summaries.db` (`conversation_summaries` table)
        2. `jetbox_summaries_proto.pb` (protobuf summary cache)
        3. `conversations/<cid>.db` (and `-wal` / `-shm`)
        4. `brain/<cid>/`
        5. `history.jsonl`
        Normal user conversations are strictly preserved.
        """
        app_dir = self._get_agy_app_data_dir()
        if not os.path.isdir(app_dir):
            return {
                "cleaned_sessions_count": 0,
                "cleaned_db_files": 0,
                "cleaned_brain_dirs": 0,
                "cleaned_pb_entries": 0,
                "cleaned_history_lines": 0,
                "remaining_sessions_count": 0,
            }

        with self._agy_track_lock:
            target_cids: Set[str] = set(self._tracked_agy_conversation_ids)

        summary_db = os.path.join(app_dir, "conversation_summaries.db")
        remaining_count = 0

        # 1. Identify & delete translation session rows in `conversation_summaries.db`
        if os.path.isfile(summary_db):
            try:
                conn = sqlite3.connect(summary_db, timeout=10.0)
                try:
                    rows = conn.execute(
                        "SELECT conversation_id, title, preview, workspace_uris, step_count FROM conversation_summaries"
                    ).fetchall()
                    for cid, title, prev, ws, sc in rows:
                        if self._is_translation_session_row(cid, title, prev, ws, sc, target_cids):
                            target_cids.add(cid)

                    if target_cids:
                        cid_list = list(target_cids)
                        for i in range(0, len(cid_list), 200):
                            batch = cid_list[i : i + 200]
                            placeholders = ",".join("?" for _ in batch)
                            conn.execute(
                                f"DELETE FROM conversation_summaries WHERE conversation_id IN ({placeholders})",
                                batch,
                            )
                        conn.commit()
                    rem_row = conn.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()
                    remaining_count = int(rem_row[0]) if rem_row else 0
                finally:
                    conn.close()
            except Exception:
                pass

        # Also scan `conversations/*.db` for any untracked sandbox translation DBs
        conv_dir = os.path.join(app_dir, "conversations")
        if os.path.isdir(conv_dir) and target_cids:
            pass

        # 2. Clean `jetbox_summaries_proto.pb`
        pb_path = os.path.join(app_dir, "jetbox_summaries_proto.pb")
        cleaned_pb = self._filter_jetbox_summaries_pb(pb_path, target_cids)

        # 3. Remove `conversations/<cid>.db*` and `brain/<cid>/`
        cleaned_db_files = 0
        cleaned_brain_dirs = 0
        brain_dir = os.path.join(app_dir, "brain")

        for cid in target_cids:
            if not cid or "/" in cid or "\\" in cid or ".." in cid:
                continue
            for sfx in (".db", ".db-wal", ".db-shm"):
                db_p = os.path.join(conv_dir, f"{cid}{sfx}")
                if os.path.isfile(db_p):
                    try:
                        os.remove(db_p)
                        if sfx == ".db":
                            cleaned_db_files += 1
                    except Exception:
                        pass
            b_p = os.path.join(brain_dir, cid)
            if os.path.isdir(b_p):
                try:
                    shutil.rmtree(b_p, ignore_errors=True)
                    cleaned_brain_dirs += 1
                except Exception:
                    pass

        # 4. Clean `history.jsonl`
        history_path = os.path.join(app_dir, "history.jsonl")
        cleaned_history = 0
        if os.path.isfile(history_path) and target_cids:
            try:
                with open(history_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                kept_lines: List[str] = []
                for line in lines:
                    drop = False
                    if any(cid in line for cid in target_cids) or "agy_sandbox" in line or "[INPUT JSON TO TRANSLATE]" in line:
                        drop = True
                    if drop:
                        cleaned_history += 1
                    else:
                        kept_lines.append(line)
                if cleaned_history > 0:
                    with open(history_path, "w", encoding="utf-8") as f:
                        f.writelines(kept_lines)
            except Exception:
                pass

        with self._agy_track_lock:
            self._tracked_agy_conversation_ids.difference_update(target_cids)

        return {
            "cleaned_sessions_count": max(len(target_cids), cleaned_pb),
            "cleaned_db_files": cleaned_db_files,
            "cleaned_brain_dirs": cleaned_brain_dirs,
            "cleaned_pb_entries": cleaned_pb,
            "cleaned_history_lines": cleaned_history,
            "remaining_sessions_count": remaining_count,
        }


