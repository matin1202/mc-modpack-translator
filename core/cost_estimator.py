"""Dynamic Chunk & Cost Simulator for Multi-Provider LLM Translation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from .scanner import ModEntry


# Fixed tokens per API request for system instructions + Minecraft format rules + filtered glossary
SYSTEM_PROMPT_BASE_TOKENS = 320
AVG_GLOSSARY_TOKENS_PER_CHUNK = 220
OVERHEAD_TOKENS_PER_CHUNK = SYSTEM_PROMPT_BASE_TOKENS + AVG_GLOSSARY_TOKENS_PER_CHUNK

# Output size relative to input payload tokens (Korean translation vs. English source).
OUTPUT_RATIO_DEFAULT = 1.15
# English bilingual annotation, e.g. "철 주괴(Iron Ingot)": proper nouns/terms get their English
# original appended in parentheses, which adds roughly 30% to the output (heuristic estimate).
OUTPUT_RATIO_BILINGUAL = 1.45
# Extra system-prompt tokens for the bilingual rule (see BILINGUAL_RULE in translator.py).
BILINGUAL_PROMPT_EXTRA_TOKENS = 120

_CJK_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ\u3040-\u30ff\u4e00-\u9fff]")


def estimate_text_tokens(text: str) -> int:
    """
    Estimate LLM token count for English, Korean, and JSON payloads in real time.
    - Hangul / CJK characters: ~1.05 tokens per character
    - ASCII / JSON punctuation / Latin letters: ~1 token per 3.7 characters
    """
    if not text:
        return 0
    total_len = len(text)
    cjk_count = len(_CJK_HANGUL_RE.findall(text))
    ascii_count = max(0, total_len - cjk_count)
    return max(1, int(round(cjk_count * 1.05 + ascii_count / 3.7)))


# Per-model standard API token rates (input_price_per_1m, output_price_per_1m) in USD
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # Gemini 4 Argon family
    "gemini-4-argon": (2.00, 12.00),
    "gemini-4-argon-low": (2.00, 12.00),
    "gemini-4-argon-medium": (2.00, 12.00),
    "gemini-4-argon-high": (2.00, 12.00),
    # Gemini 3.1 Pro & 2.5/1.5 Pro family
    "gemini-3.1-pro": (1.25, 10.00),
    "gemini-3.1-pro-preview": (1.25, 10.00),
    "gemini-3.1-pro-low": (1.25, 10.00),
    "gemini-3.1-pro-medium": (1.25, 10.00),
    "gemini-3.1-pro-high": (1.25, 10.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-1.5-pro": (1.25, 5.00),
    # Gemini 3.8 / 2.5 / 2.0 Flash family
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.8-flash-low": (0.75, 3.75),
    "gemini-3.8-flash-medium": (0.75, 3.75),
    "gemini-3.8-flash-high": (0.75, 3.75),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-1.5-flash": (0.075, 0.30),
    # Gemini Flash Lite family
    "gemini-3.5-flash-lite": (0.0375, 0.15),
    "gemini-2.5-flash-lite": (0.0375, 0.15),
    "gemini-2.0-flash-lite": (0.0375, 0.15),
    "gemini-1.5-flash-8b": (0.0375, 0.15),
    # Anthropic Claude family
    "claude-sonnet-5-5-low": (3.00, 15.00),
    "claude-sonnet-5-5-medium": (3.00, 15.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-opus-5-5-medium": (15.00, 75.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    # OpenAI GPT family
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-oss-120b-medium": (0.20, 0.60),
}


@dataclass
class ProviderSpec:
    provider_id: str
    name: str
    default_model: str
    available_models: List[str]
    input_price_per_1m: float        # USD per 1M input tokens
    output_price_per_1m: float       # USD per 1M output tokens
    long_context_threshold: int      # e.g. 128000 tokens
    long_context_multiplier: float   # e.g. 2.0x if chunk exceeds 128k
    max_output_tokens: int           # Max output tokens per single completion
    recommended_max_chunk_keys: int  # Safe upper bound of keys per chunk
    default_concurrency: int         # Recommended default parallel workers
    max_concurrency: int             # Safe maximum parallel workers
    init_latency_sec: float          # Avg startup/network latency per chunk request
    output_tokens_per_sec: float     # Avg output token streaming speed per worker
    badge: str
    requires_api_key: bool


PROVIDERS: Dict[str, ProviderSpec] = {
    "agy": ProviderSpec(
        provider_id="agy",
        name="AGY CLI (Antigravity Agent)",
        default_model="gemini-3.8-flash-low",
        available_models=[
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.1-pro-low",
            "gemini-3.1-pro-medium",
            "gemini-3.1-pro-high",
            "gemini-4-argon-low",
            "gemini-4-argon-medium",
            "gemini-4-argon-high",
            "claude-sonnet-5-5-low",
            "claude-sonnet-5-5-medium",
            "claude-opus-5-5-medium",
            "gpt-oss-120b-medium",
        ],
        input_price_per_1m=0.0,
        output_price_per_1m=0.0,
        long_context_threshold=200000,
        long_context_multiplier=1.0,
        max_output_tokens=65536,
        recommended_max_chunk_keys=2500,
        default_concurrency=3,
        max_concurrency=6,
        init_latency_sec=4.0,
        output_tokens_per_sec=90.0,
        badge="구독 포함 ($0) · Argon / 3.1 Pro 지원",
        requires_api_key=False,
    ),
    "gemini_argon": ProviderSpec(
        provider_id="gemini_argon",
        name="Google Gemini 4 Argon",
        default_model="gemini-4-argon-medium",
        available_models=[
            "gemini-4-argon-medium",
            "gemini-4-argon-low",
            "gemini-4-argon-high",
            "gemini-4-argon",
        ],
        input_price_per_1m=2.00,
        output_price_per_1m=12.00,
        long_context_threshold=200000,
        long_context_multiplier=2.0,
        max_output_tokens=65536,
        recommended_max_chunk_keys=2500,
        default_concurrency=4,
        max_concurrency=10,
        init_latency_sec=2.0,
        output_tokens_per_sec=95.0,
        badge="💎 차세대 플래그십 Argon · 64K 출력",
        requires_api_key=True,
    ),
    "gemini_pro": ProviderSpec(
        provider_id="gemini_pro",
        name="Google Gemini 3.1 Pro",
        default_model="gemini-3.1-pro",
        available_models=[
            "gemini-3.1-pro",
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-low",
            "gemini-3.1-pro-medium",
            "gemini-3.1-pro-high",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
        ],
        input_price_per_1m=1.25,
        output_price_per_1m=10.00,
        long_context_threshold=200000,
        long_context_multiplier=2.0,
        max_output_tokens=65536,
        recommended_max_chunk_keys=2000,
        default_concurrency=4,
        max_concurrency=10,
        init_latency_sec=2.2,
        output_tokens_per_sec=80.0,
        badge="🧠 고품질 3.1 Pro · 64K 출력 · 정밀 문맥",
        requires_api_key=True,
    ),
    "gemini_flash": ProviderSpec(
        provider_id="gemini_flash",
        name="Google Gemini 3.8 / 2.0 Flash",
        default_model="gemini-3.8-flash",
        available_models=["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
        input_price_per_1m=0.75,
        output_price_per_1m=3.75,
        long_context_threshold=128000,
        long_context_multiplier=2.0,
        max_output_tokens=65536,
        recommended_max_chunk_keys=2500,
        default_concurrency=5,
        max_concurrency=12,
        init_latency_sec=1.6,
        output_tokens_per_sec=125.0,
        badge="⚡ 64K 초대용량 · 고속 병렬",
        requires_api_key=True,
    ),
    "gemini_lite": ProviderSpec(
        provider_id="gemini_lite",
        name="Google Gemini 3.5 Flash Lite",
        default_model="gemini-3.5-flash-lite",
        available_models=["gemini-3.5-flash-lite", "gemini-2.0-flash-lite", "gemini-1.5-flash-8b"],
        input_price_per_1m=0.0375,
        output_price_per_1m=0.15,
        long_context_threshold=128000,
        long_context_multiplier=1.5,
        max_output_tokens=65536,
        recommended_max_chunk_keys=2000,
        default_concurrency=6,
        max_concurrency=12,
        init_latency_sec=1.2,
        output_tokens_per_sec=150.0,
        badge="🔥 64K 출력 / 최저가 · 초고속 병렬",
        requires_api_key=True,
    ),
    "openai_mini": ProviderSpec(
        provider_id="openai_mini",
        name="OpenAI GPT-4o-mini",
        default_model="gpt-4o-mini",
        available_models=["gpt-4o-mini", "gpt-4.1-mini"],
        input_price_per_1m=0.15,
        output_price_per_1m=0.60,
        long_context_threshold=128000,
        long_context_multiplier=1.0,
        max_output_tokens=16384,
        recommended_max_chunk_keys=700,
        default_concurrency=4,
        max_concurrency=10,
        init_latency_sec=1.5,
        output_tokens_per_sec=80.0,
        badge="💡 안정적 JSON · 병렬 지원",
        requires_api_key=True,
    ),
    "openai_4o": ProviderSpec(
        provider_id="openai_4o",
        name="OpenAI GPT-4o",
        default_model="gpt-4o",
        available_models=["gpt-4o", "gpt-4.1"],
        input_price_per_1m=2.50,
        output_price_per_1m=10.00,
        long_context_threshold=128000,
        long_context_multiplier=1.0,
        max_output_tokens=16384,
        recommended_max_chunk_keys=700,
        default_concurrency=3,
        max_concurrency=8,
        init_latency_sec=2.0,
        output_tokens_per_sec=55.0,
        badge="고품질 스토리/퀘스트",
        requires_api_key=True,
    ),
    "claude_haiku": ProviderSpec(
        provider_id="claude_haiku",
        name="Anthropic Claude 3.5 Haiku",
        default_model="claude-3-5-haiku-latest",
        available_models=["claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"],
        input_price_per_1m=0.80,
        output_price_per_1m=4.00,
        long_context_threshold=200000,
        long_context_multiplier=1.0,
        max_output_tokens=8192,
        recommended_max_chunk_keys=450,
        default_concurrency=3,
        max_concurrency=8,
        init_latency_sec=1.6,
        output_tokens_per_sec=85.0,
        badge="자연스러운 한국어",
        requires_api_key=True,
    ),
    "ollama": ProviderSpec(
        provider_id="ollama",
        name="Ollama (로컬 LLM)",
        default_model="qwen2.5:14b",
        available_models=["qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b", "gemma2:9b"],
        input_price_per_1m=0.0,
        output_price_per_1m=0.0,
        long_context_threshold=128000,
        long_context_multiplier=1.0,
        max_output_tokens=8192,
        recommended_max_chunk_keys=300,
        default_concurrency=1,
        max_concurrency=2,
        init_latency_sec=1.0,
        output_tokens_per_sec=35.0,
        badge="🔒 완전 무료 (로컬 GPU)",
        requires_api_key=False,
    ),
    "deepl": ProviderSpec(
        provider_id="deepl",
        name="DeepL API (Free / Pro)",
        default_model="deepl-free",
        available_models=["deepl-free", "deepl-pro"],
        input_price_per_1m=0.0,   # Handled separately per character
        output_price_per_1m=0.0,
        long_context_threshold=128000,
        long_context_multiplier=1.0,
        max_output_tokens=32000,
        recommended_max_chunk_keys=500,
        default_concurrency=4,
        max_concurrency=8,
        init_latency_sec=0.8,
        output_tokens_per_sec=550.0,
        badge="월 50만자 무료 · 초고속",
        requires_api_key=True,
    ),
}


def resolve_model_pricing(provider_id: str, model: str = "") -> Tuple[float, float, float, float, bool]:
    """
    Return `(billed_in_per_1m, billed_out_per_1m, equiv_in_per_1m, equiv_out_per_1m, is_free_tier)`
    for a given `provider_id` and `model`.
    - For paid API providers, `billed_*` and `equiv_*` both match the model's exact price.
    - For `agy` (AGY CLI subscription) and `ollama` (local), all rates are 0.0 (no cost calculation).
    """
    if provider_id in ("agy", "ollama"):
        return 0.0, 0.0, 0.0, 0.0, True

    spec = PROVIDERS.get(provider_id)
    m = (model or (spec.default_model if spec else "")).strip().lower()

    equiv_in, equiv_out = (spec.input_price_per_1m, spec.output_price_per_1m) if spec else (0.10, 0.40)
    if m in MODEL_PRICING:
        equiv_in, equiv_out = MODEL_PRICING[m]
    else:
        for prefix, rates in MODEL_PRICING.items():
            if m.startswith(prefix):
                equiv_in, equiv_out = rates
                break

    if provider_id == "deepl":
        is_free = m == "deepl-free"
        return 0.0, 0.0, 0.0, 0.0, is_free
    return equiv_in, equiv_out, equiv_in, equiv_out, False


def calculate_realtime_cost(
    provider_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_chars: int = 0,
    exchange_rate_krw: float = 1400.0,
) -> dict:
    """
    Calculate real-time input/output token costs (both actual billed cost and API-equivalent cost)
    in USD and KRW.
    """
    in_tok = max(0, int(input_tokens))
    out_tok = max(0, int(output_tokens))
    tot_tok = in_tok + out_tok

    billed_in_rate, billed_out_rate, equiv_in_rate, equiv_out_rate, is_free = resolve_model_pricing(
        provider_id, model
    )

    if provider_id == "deepl":
        pro_usd = (max(0, total_chars) / 1_000_000.0) * 20.0
        billed_usd = 0.0 if (is_free and total_chars <= 500_000) else pro_usd
        in_cost_usd = billed_usd
        out_cost_usd = 0.0
        equiv_in_usd = pro_usd
        equiv_out_usd = 0.0
        equiv_total_usd = pro_usd
    else:
        in_cost_usd = (in_tok / 1_000_000.0) * billed_in_rate
        out_cost_usd = (out_tok / 1_000_000.0) * billed_out_rate
        billed_usd = in_cost_usd + out_cost_usd

        equiv_in_usd = (in_tok / 1_000_000.0) * equiv_in_rate
        equiv_out_usd = (out_tok / 1_000_000.0) * equiv_out_rate
        equiv_total_usd = equiv_in_usd + equiv_out_usd

    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": tot_tok,
        "input_price_per_1m": equiv_in_rate,
        "output_price_per_1m": equiv_out_rate,
        "billed_input_cost_usd": round(in_cost_usd, 6),
        "billed_output_cost_usd": round(out_cost_usd, 6),
        "billed_total_cost_usd": round(billed_usd, 6),
        "billed_total_cost_krw": round(billed_usd * exchange_rate_krw, 1),
        "equiv_input_cost_usd": round(equiv_in_usd, 6),
        "equiv_output_cost_usd": round(equiv_out_usd, 6),
        "equiv_total_cost_usd": round(equiv_total_usd, 6),
        "equiv_total_cost_krw": round(equiv_total_usd * exchange_rate_krw, 1),
        "is_free_tier": is_free,
    }


def format_duration_ko(seconds: float) -> str:
    """Format seconds into a human-friendly Korean duration string."""
    if seconds <= 0:
        return "0초"
    sec = int(round(seconds))
    if sec < 60:
        return f"약 {max(1, sec)}초"
    mins, rem_sec = divmod(sec, 60)
    if mins < 60:
        return f"약 {mins}분 {rem_sec}초" if rem_sec > 0 else f"약 {mins}분"
    hours, rem_mins = divmod(mins, 60)
    return f"약 {hours}시간 {rem_mins}분"


def simulate_chunk_and_cost(
    entries: List[ModEntry],
    chunk_size: int = 250,
    concurrency: int = 3,
    deduplicate: bool = True,
    translate_all: bool = False,
    pack_small_mods: bool = True,
    exchange_rate_krw: float = 1400.0,
    bilingual: bool = False,
) -> dict:
    """
    Calculate detailed token, cost, and estimated duration breakdown for a given list
    of ModEntries, chunk_size, concurrency level, and multi-mod batch packing (`pack_small_mods`).
    When `bilingual` is set, larger output (English annotations) and a longer system prompt are assumed.
    """
    output_ratio = OUTPUT_RATIO_BILINGUAL if bilingual else OUTPUT_RATIO_DEFAULT
    overhead_per_chunk = OVERHEAD_TOKENS_PER_CHUNK + (BILINGUAL_PROMPT_EXTRA_TOKENS if bilingual else 0)
    chunk_size = max(10, min(5000, int(chunk_size)))
    concurrency = max(1, min(16, int(concurrency)))

    total_mods = len(entries)
    total_raw_keys = 0
    total_effective_keys = 0
    total_payload_input_tokens = 0
    total_raw_input_tokens = 0
    total_chars = 0

    unpacked_chunks = 0
    packed_chunks = 0
    baseline_small_chunks = 0  # Reference: chunk_size = 50 (unpacked)
    max_single_chunk_input_tokens = 0
    max_single_chunk_output_tokens = 0

    buf_keys = 0
    buf_chars = 0.0
    buf_tok = 0.0

    def flush_packed_buffer():
        nonlocal packed_chunks, buf_keys, buf_chars, buf_tok, max_single_chunk_input_tokens, max_single_chunk_output_tokens
        if buf_keys <= 0:
            return
        packed_chunks += 1
        c_in = int(buf_tok) + overhead_per_chunk
        c_out = int(buf_tok * (output_ratio + 0.03)) + 25
        if c_in > max_single_chunk_input_tokens:
            max_single_chunk_input_tokens = c_in
        if c_out > max_single_chunk_output_tokens:
            max_single_chunk_output_tokens = c_out
        buf_keys = 0
        buf_chars = 0.0
        buf_tok = 0.0

    for e in entries:
        if translate_all:
            raw_k = e.total_keys
            eff_k = e.total_keys
            in_tok = e.total_all_tokens
            raw_tok = e.total_all_tokens
        else:
            raw_k = e.untranslated_keys
            eff_k = e.unique_untranslated if deduplicate else e.untranslated_keys
            in_tok = e.estimated_tokens if deduplicate else e.raw_estimated_tokens
            raw_tok = e.raw_estimated_tokens

        if eff_k <= 0:
            continue

        total_raw_keys += raw_k
        total_effective_keys += eff_k
        total_payload_input_tokens += in_tok
        total_raw_input_tokens += raw_tok
        total_chars += e.total_chars

        # Match adaptive chunking (both key limit and 22,000 char limit per chunk)
        est_entry_chars = int(e.total_chars * (eff_k / max(1, e.total_keys)))
        chunks_by_keys = math.ceil(eff_k / chunk_size)
        chunks_by_chars = math.ceil(est_entry_chars / 22000) if est_entry_chars > 0 else 1
        mod_chunks = max(chunks_by_keys, chunks_by_chars)
        mod_small_chunks = max(math.ceil(eff_k / 50), chunks_by_chars)

        unpacked_chunks += mod_chunks
        baseline_small_chunks += mod_small_chunks

        avg_tok_per_key = in_tok / max(1, eff_k)
        avg_chars_per_key = est_entry_chars / max(1, eff_k)

        if not pack_small_mods:
            packed_chunks += mod_chunks
            largest_chunk_keys_in_mod = math.ceil(eff_k / max(1, mod_chunks))
            chunk_payload_tok = int(largest_chunk_keys_in_mod * avg_tok_per_key)
            chunk_in_tok = chunk_payload_tok + overhead_per_chunk
            chunk_out_tok = int(chunk_payload_tok * (output_ratio + 0.03)) + 25
            if chunk_in_tok > max_single_chunk_input_tokens:
                max_single_chunk_input_tokens = chunk_in_tok
            if chunk_out_tok > max_single_chunk_output_tokens:
                max_single_chunk_output_tokens = chunk_out_tok
        else:
            rem_k = eff_k
            while rem_k > 0:
                avail_by_k = chunk_size - buf_keys
                avail_by_c = (
                    int((22000.0 - buf_chars) / max(1.0, avg_chars_per_key))
                    if avg_chars_per_key > 0
                    else avail_by_k
                )
                take_k = min(rem_k, avail_by_k, max(0, avail_by_c))
                if take_k <= 0:
                    if buf_keys == 0:
                        take_k = 1
                    else:
                        flush_packed_buffer()
                        continue
                buf_keys += take_k
                buf_chars += take_k * avg_chars_per_key
                buf_tok += take_k * avg_tok_per_key
                rem_k -= take_k
                if buf_keys >= chunk_size or buf_chars >= 21000:
                    flush_packed_buffer()

    if pack_small_mods and buf_keys > 0:
        flush_packed_buffer()

    total_chunks = packed_chunks
    saved_requests_by_packing = max(0, unpacked_chunks - packed_chunks)

    # Typical (weighted average) tokens per chunk across selected entries
    typical_chunk_output_tokens = (
        int((total_payload_input_tokens / max(1, total_chunks)) * output_ratio) + 20
        if total_chunks > 0
        else 0
    )

    # Prompt overhead calculation
    prompt_overhead_tokens = total_chunks * overhead_per_chunk
    baseline_overhead_tokens = baseline_small_chunks * overhead_per_chunk
    overhead_saved_tokens = max(0, baseline_overhead_tokens - prompt_overhead_tokens)
    overhead_saved_pct = (
        round((overhead_saved_tokens / baseline_overhead_tokens) * 100, 1)
        if baseline_overhead_tokens > 0
        else 0.0
    )

    total_input_tokens = total_payload_input_tokens + prompt_overhead_tokens
    total_output_tokens = int(total_payload_input_tokens * output_ratio)

    # Calculate per-provider costs, estimated duration, and chunk safety status
    provider_estimates = []
    for pid, spec in PROVIDERS.items():
        # Check if long context multiplier applies
        multiplier = (
            spec.long_context_multiplier
            if max_single_chunk_input_tokens > spec.long_context_threshold
            else 1.0
        )

        if pid == "deepl":
            # DeepL Free: $0 up to 500k chars; Pro: $20 / 1M chars
            pro_cost_usd = (total_chars / 1_000_000.0) * 20.0
            in_cost = 0.0 if total_chars <= 500_000 else pro_cost_usd
            out_cost = 0.0
            cost_usd = in_cost
        else:
            in_cost = (total_input_tokens / 1_000_000.0) * spec.input_price_per_1m * multiplier
            out_cost = (total_output_tokens / 1_000_000.0) * spec.output_price_per_1m * multiplier
            cost_usd = in_cost + out_cost

        cost_krw = round(cost_usd * exchange_rate_krw)

        # Build per-model cost lookup (so selecting a model like gemini-4-argon or gemini-3.1-pro shows its exact API value)
        model_costs = {}
        for m_name in spec.available_models:
            m_rt = calculate_realtime_cost(
                provider_id=pid,
                model=m_name,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                total_chars=total_chars,
                exchange_rate_krw=exchange_rate_krw,
            )
            model_costs[m_name] = m_rt

        # Estimated time calculation (Serial vs Concurrent)
        eff_workers = min(concurrency, spec.max_concurrency)
        if total_chunks <= 0 or total_effective_keys <= 0:
            serial_sec = 0.0
            parallel_sec = 0.0
            speedup = 1.0
        else:
            serial_sec = (total_chunks * spec.init_latency_sec) + (
                total_output_tokens / max(1.0, spec.output_tokens_per_sec)
            )
            active_workers = min(eff_workers, total_chunks)
            # Wave-based parallel model: accounts for tail latency when total_chunks is not a multiple of active_workers
            waves = math.ceil(total_chunks / active_workers)
            avg_chunk_sec = serial_sec / total_chunks
            # Blend pure division and wave ceiling (since chunk sizes vary)
            parallel_sec = (serial_sec / active_workers) * 0.75 + (waves * avg_chunk_sec) * 0.25
            speedup = round(serial_sec / max(0.1, parallel_sec), 1)

        # Output token limit safety check based on typical chunk output tokens
        utilization_pct = round((typical_chunk_output_tokens / max(1, spec.max_output_tokens)) * 100, 1)
        if typical_chunk_output_tokens > spec.max_output_tokens * 0.90:
            safety_status = "danger"
            safety_msg = f"청크 평균 출력({typical_chunk_output_tokens:,} 토큰)이 한도({spec.max_output_tokens:,})를 초과할 수 있습니다! (권장: {spec.recommended_max_chunk_keys}키 이하)"
        elif typical_chunk_output_tokens > spec.max_output_tokens * 0.65:
            safety_status = "warning"
            safety_msg = f"대용량 청크 (출력 한도의 {utilization_pct}% 사용). 장문 가이드북은 자동 분할됩니다."
        else:
            safety_status = "safe"
            safety_msg = f"안전 (평균 {typical_chunk_output_tokens:,} / {spec.max_output_tokens:,} 토큰, {utilization_pct}%)"

        provider_estimates.append({
            "provider_id": pid,
            "name": spec.name,
            "default_model": spec.default_model,
            "available_models": spec.available_models,
            "badge": spec.badge,
            "requires_api_key": spec.requires_api_key,
            "input_price_per_1m": spec.input_price_per_1m,
            "output_price_per_1m": spec.output_price_per_1m,
            "input_cost_usd": round(in_cost, 4),
            "output_cost_usd": round(out_cost, 4),
            "cost_usd": round(cost_usd, 4),
            "cost_krw": cost_krw,
            "model_costs": model_costs,
            "max_output_tokens": spec.max_output_tokens,
            "recommended_max_chunk_keys": spec.recommended_max_chunk_keys,
            "default_concurrency": spec.default_concurrency,
            "max_concurrency": spec.max_concurrency,
            "effective_concurrency": eff_workers,
            "serial_estimated_sec": round(serial_sec, 1),
            "estimated_sec": round(parallel_sec, 1),
            "estimated_time_text": format_duration_ko(parallel_sec),
            "serial_time_text": format_duration_ko(serial_sec),
            "speedup_factor": speedup,
            "chunk_output_utilization_pct": utilization_pct,
            "safety_status": safety_status,
            "safety_msg": safety_msg,
            "long_context_applied": multiplier > 1.0,
        })

    return {
        "chunk_size": chunk_size,
        "concurrency": concurrency,
        "deduplicate": deduplicate,
        "translate_all": translate_all,
        "pack_small_mods": pack_small_mods,
        "bilingual": bilingual,
        "total_mods": total_mods,
        "total_raw_keys": total_raw_keys,
        "total_effective_keys": total_effective_keys,
        "dedup_saved_keys": max(0, total_raw_keys - total_effective_keys),
        "total_chunks": total_chunks,
        "unpacked_chunks": unpacked_chunks,
        "saved_requests_by_packing": saved_requests_by_packing,
        "baseline_small_chunks": baseline_small_chunks,
        "total_payload_input_tokens": total_payload_input_tokens,
        "prompt_overhead_tokens": prompt_overhead_tokens,
        "baseline_overhead_tokens": baseline_overhead_tokens,
        "overhead_saved_tokens": overhead_saved_tokens,
        "overhead_saved_pct": overhead_saved_pct,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "max_single_chunk_input_tokens": max_single_chunk_input_tokens,
        "max_single_chunk_output_tokens": typical_chunk_output_tokens,
        "worst_case_chunk_output_tokens": max_single_chunk_output_tokens,
        "providers": provider_estimates,
    }

