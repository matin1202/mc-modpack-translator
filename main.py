"""Main Entry Point: Web Studio Server & CLI for Minecraft Modpack Translator."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(BASE_DIR)

if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from mc_translator.core.cache import TranslationCache
from mc_translator.core.cost_estimator import simulate_chunk_and_cost
from mc_translator.core.glossary import GlossaryManager
from mc_translator.core.packager import ModpackPackager
from mc_translator.core.scanner import ModEntry, ModpackScanner
from mc_translator.core.translator import MultiProviderTranslator


class AppController:
    """Holds application state, background translation worker, and services."""

    def __init__(self, default_modpack_path: str):
        self.db_path = os.path.join(BASE_DIR, "data", "translation_cache.db")
        self.glossary_dir = os.path.join(BASE_DIR, "glossaries")
        self.web_dir = os.path.join(BASE_DIR, "web")

        self.cache = TranslationCache(self.db_path)
        self.glossary = GlossaryManager(self.glossary_dir)

        saved_path = self.cache.get_setting("modpack_path", default_modpack_path)
        if not os.path.exists(saved_path) and os.path.exists(default_modpack_path):
            saved_path = default_modpack_path

        self.modpack_path = saved_path
        self.scanner = ModpackScanner(self.modpack_path)
        self.translator = MultiProviderTranslator(self.scanner, self.glossary, self.cache)
        self.packager = ModpackPackager(self.scanner, self.cache)

        # Background job state
        self.job_lock = threading.Lock()
        self.job_running = False
        self.job_just_finished = False
        self.job_logs: List[str] = []
        self.job_samples: List[Dict[str, str]] = []
        self.job_summary: dict = {}
        self.job_stream: dict = {}

        # Initial scan & TM seed if empty
        self.rescan(seed_tm=(self.cache.get_stats()["tm_unique_pairs_count"] == 0))

    def rescan(self, modpack_path: Optional[str] = None, seed_tm: bool = False) -> dict:
        if modpack_path and modpack_path.strip():
            self.modpack_path = os.path.abspath(modpack_path.strip())
            self.cache.set_setting("modpack_path", self.modpack_path)
            self.scanner = ModpackScanner(self.modpack_path)
            self.translator = MultiProviderTranslator(self.scanner, self.glossary, self.cache)
            self.packager = ModpackPackager(self.scanner, self.cache)

        # Ensure any newly added JSON files in data/mod_archive/ are synced before scanning
        self.cache._sync_archive_files_on_startup()
        all_cached_with_en = self.cache.get_all_entry_translations_with_en()
        self.scanner.scan_all(all_cached_with_en)

        if seed_tm:
            tm_pairs = self.scanner.extract_builtin_tm_pairs()
            self.cache.seed_translation_memory(tm_pairs, source="builtin_jar")

        return self.get_state_payload()

    def get_state_payload(self) -> dict:
        entries_list = sorted(
            self.scanner.entries.values(),
            key=lambda e: (e.untranslated_keys == 0, -e.untranslated_keys, e.display_name.lower()),
        )
        total_keys = sum(e.total_keys for e in entries_list)
        builtin_ko = sum(e.builtin_ko_keys for e in entries_list)
        cached_ko = sum(e.cached_ko_keys for e in entries_list)
        ver_changed = sum(e.version_changed_keys for e in entries_list)
        untrans_keys = sum(e.untranslated_keys for e in entries_list)
        unique_untrans = sum(e.unique_untranslated for e in entries_list)
        done_keys = min(total_keys, builtin_ko + cached_ko)
        overall_pct = round((done_keys / total_keys) * 100, 1) if total_keys > 0 else 100.0
        db_stats = self.cache.get_stats()
        agy_stats = self.translator.get_agy_translation_sessions_stats()

        return {
            "modpack_path": self.modpack_path,
            "mc_root": self.scanner.mc_root,
            "modpack_info": self.scanner.modpack_info,
            "default_export_dir": self.packager.default_export_dir,
            "export_history": self.packager.list_exported_history(),
            "mod_archive_dir": db_stats.get("mod_archive_dir", ""),
            "agy_session_stats": agy_stats,
            "entries": [e.to_dict() for e in entries_list],
            "totals": {
                "total_entries": len(entries_list),
                "total_keys": total_keys,
                "builtin_ko_keys": builtin_ko,
                "cached_ko_keys": cached_ko,
                "version_changed_keys": ver_changed,
                "untranslated_keys": untrans_keys,
                "unique_untranslated": unique_untrans,
                "overall_progress_pct": overall_pct,
                "tm_unique_pairs_count": db_stats["tm_unique_pairs_count"],
                "archive_files_count": db_stats.get("archive_files_count", 0),
                "translated_entries_count": db_stats.get("translated_entries_count", 0),
            },
            "settings": self.cache.get_all_settings(),
        }

    def start_job(self, params: dict) -> dict:
        with self.job_lock:
            if self.job_running:
                return {"error": "이미 번역 작업이 실행 중입니다."}
            self.job_running = True
            self.job_just_finished = False
            self.job_logs = []
            self.job_samples = []
            self.job_summary = {}
            self.job_stream = {}

        entry_ids = params.get("entry_ids") or []
        provider_id = params.get("provider_id") or "agy"
        model = params.get("model") or "gemini-3.8-flash-low"
        api_key = params.get("api_key") or self.cache.get_setting(f"apikey_{provider_id}", "")
        ollama_url = params.get("ollama_url") or self.cache.get_setting("ollama_url", "http://localhost:11434")
        chunk_size = int(params.get("chunk_size") or 300)
        concurrency = int(params.get("concurrency") or 3)
        deduplicate = bool(params.get("deduplicate", True))
        use_tm_prematch = bool(params.get("use_tm_prematch", True))
        translate_all = bool(params.get("translate_all", False))
        pack_small_mods = bool(params.get("pack_small_mods", True))
        auto_clean_agy_sessions = bool(params.get("auto_clean_agy_sessions", True))
        bilingual = bool(params.get("bilingual", False))

        def on_progress(ev: dict):
            with self.job_lock:
                msg = ev.get("message", "")
                if msg:
                    self.job_logs.append(msg)
                    if len(self.job_logs) > 500:
                        self.job_logs = self.job_logs[-500:]
                if "summary" in ev:
                    self.job_summary = ev["summary"]
                if "stream" in ev and ev["stream"]:
                    self.job_stream = ev["stream"]
                if "samples" in ev and ev["samples"]:
                    # Prepend new samples while avoiding duplicates by key
                    existing_by_k = {s["key"]: s for s in self.job_samples}
                    for s in ev["samples"]:
                        existing_by_k.pop(s["key"], None)
                    self.job_samples = (ev["samples"] + list(existing_by_k.values()))[:30]

        def worker():
            try:
                self.translator.translate_entries(
                    entry_ids=entry_ids,
                    provider_id=provider_id,
                    model=model,
                    api_key=api_key,
                    ollama_url=ollama_url,
                    chunk_size=chunk_size,
                    concurrency=concurrency,
                    deduplicate=deduplicate,
                    use_tm_prematch=use_tm_prematch,
                    translate_all=translate_all,
                    pack_small_mods=pack_small_mods,
                    auto_clean_agy_sessions=auto_clean_agy_sessions,
                    bilingual=bilingual,
                    progress_callback=on_progress,
                )
            finally:
                with self.job_lock:
                    self.job_running = False
                    self.job_just_finished = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return {"started": True}

    def get_job_status(self) -> dict:
        with self.job_lock:
            just_fin = self.job_just_finished
            self.job_just_finished = False
            return {
                "running": self.job_running,
                "just_finished": just_fin,
                "logs": list(self.job_logs),
                "samples": list(self.job_samples),
                "summary": dict(self.job_summary),
                "stream": dict(self.job_stream),
            }


def create_request_handler(controller: AppController):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Quiet standard HTTP logs
            pass

        def _send_json(self, data: dict, status: int = 200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                html_path = os.path.join(controller.web_dir, "index.html")
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

            if path == "/api/state":
                self._send_json(controller.get_state_payload())
                return

            if path == "/api/translate/status":
                self._send_json(controller.get_job_status())
                return

            if path == "/api/glossary":
                self._send_json(controller.glossary.get_summary())
                return

            if path == "/api/review":
                qs = urllib.parse.parse_qs(parsed.query)
                entry_id = (qs.get("entry_id") or [""])[0]
                en_dict = controller.scanner.get_en_dict(entry_id)
                builtin_ko = controller.scanner.get_builtin_ko_dict(entry_id)
                cached_with_en = controller.cache.get_entry_translations_with_en(entry_id)
                mod_en_to_ko = {
                    s_en: s_ko
                    for s_en, s_ko in cached_with_en.values()
                    if s_en and s_ko and s_ko.strip()
                }
                rows = []
                for k, en_val in en_dict.items():
                    ko_val = ""
                    status = "untranslated"
                    old_en = ""
                    if k in builtin_ko and builtin_ko[k].strip():
                        ko_val = builtin_ko[k]
                        status = "builtin"
                    elif k in cached_with_en:
                        saved_en, saved_ko = cached_with_en[k]
                        if saved_ko and saved_ko.strip():
                            if not saved_en or saved_en == en_val:
                                ko_val = saved_ko
                                status = "cached"
                            elif en_val in mod_en_to_ko:
                                ko_val = mod_en_to_ko[en_val]
                                status = "cached"
                            else:
                                ko_val = saved_ko
                                old_en = saved_en
                                status = "version_changed"
                    elif en_val in mod_en_to_ko:
                        ko_val = mod_en_to_ko[en_val]
                        status = "cached"
                    rows.append({
                        "key": k,
                        "en": en_val,
                        "ko": ko_val,
                        "status": status,
                        "old_en": old_en,
                    })
                self._send_json({"entry_id": entry_id, "rows": rows})
                return

            if path == "/api/export/download":
                qs = urllib.parse.parse_qs(parsed.query)
                raw_file = (qs.get("file") or [""])[0].strip()
                custom_dir = (qs.get("dir") or [""])[0]
                target_dir = os.path.abspath(
                    custom_dir.strip()
                    if custom_dir and custom_dir.strip()
                    else controller.packager.default_export_dir
                )
                if not raw_file:
                    self._send_json({"error": "파일을 찾을 수 없습니다."}, 404)
                    return

                # Allow `<Modpack>/<Version>/<filename>` relative paths inside `target_dir`,
                # while strictly preventing directory traversal outside `target_dir`.
                full_path = os.path.abspath(os.path.join(target_dir, raw_file.replace("/", os.sep)))
                try:
                    common = os.path.commonpath([target_dir, full_path])
                except ValueError:
                    common = ""
                if common != target_dir or not os.path.isfile(full_path):
                    self._send_json({"error": "파일을 찾을 수 없습니다."}, 404)
                    return

                fname = os.path.basename(full_path)
                with open(full_path, "rb") as f:
                    data_bytes = f.read()
                ctype = "application/json" if fname.endswith(".json") else "application/zip"
                encoded_fname = urllib.parse.quote(fname)
                ascii_fname = fname.encode("ascii", errors="ignore").decode("ascii").strip("_ ") or "export_bundle"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data_bytes)))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=\"{ascii_fname}\"; filename*=UTF-8''{encoded_fname}",
                )
                self.end_headers()
                self.wfile.write(data_bytes)
                return

            self._send_json({"error": "Not found"}, 404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_json_body()

            if path == "/api/scan":
                res = controller.rescan(
                    modpack_path=body.get("modpack_path"),
                    seed_tm=bool(body.get("seed_tm", False)),
                )
                self._send_json(res)
                return

            if path == "/api/simulate":
                entry_ids = body.get("entry_ids") or []
                if entry_ids:
                    selected = [
                        controller.scanner.entries[eid]
                        for eid in entry_ids
                        if eid in controller.scanner.entries
                    ]
                else:
                    # If nothing checked yet, simulate for all untranslated entries (or all entries if translate_all)
                    inc_all = bool(body.get("translate_all", False))
                    selected = [
                        e for e in controller.scanner.entries.values()
                        if (e.total_keys > 0 if inc_all else e.untranslated_keys > 0)
                    ]
                sim = simulate_chunk_and_cost(
                    entries=selected,
                    chunk_size=int(body.get("chunk_size") or 300),
                    concurrency=int(body.get("concurrency") or 3),
                    deduplicate=bool(body.get("deduplicate", True)),
                    translate_all=bool(body.get("translate_all", False)),
                    pack_small_mods=bool(body.get("pack_small_mods", True)),
                    bilingual=bool(body.get("bilingual", False)),
                )
                self._send_json(sim)
                return

            if path == "/api/settings":
                for k, v in body.items():
                    controller.cache.set_setting(str(k), str(v))
                self._send_json({"ok": True})
                return

            if path == "/api/translate/start":
                res = controller.start_job(body)
                self._send_json(res)
                return

            if path == "/api/translate/stop":
                controller.translator.request_cancel()
                self._send_json({"stopped": True})
                return

            if path == "/api/agy/cleanup":
                clean_res = controller.translator.cleanup_agy_sessions()
                clean_res["agy_session_stats"] = controller.translator.get_agy_translation_sessions_stats()
                self._send_json(clean_res)
                return

            if path == "/api/glossary/add":
                controller.glossary.add_or_update_custom_term(
                    original=body.get("original", ""),
                    translated=body.get("translated", ""),
                    mod_id=body.get("mod_id", "global"),
                    note=body.get("note", ""),
                )
                self._send_json({"ok": True})
                return

            if path == "/api/glossary/delete":
                controller.glossary.delete_custom_term(
                    original=body.get("original", ""),
                    mod_id=body.get("mod_id", "global"),
                )
                self._send_json({"ok": True})
                return

            if path == "/api/glossary/import_csv":
                cnt = controller.glossary.import_csv_text(body.get("csv_text", ""))
                self._send_json({"imported_count": cnt})
                return

            if path == "/api/review/save":
                entry_id = body.get("entry_id", "")
                lang_key = body.get("lang_key", "")
                en_text = body.get("en_text", "")
                ko_text = body.get("ko_text", "")
                if entry_id and lang_key:
                    controller.cache.save_translations_batch(
                        entry_id, [(lang_key, en_text, ko_text)], provider="manual_edit"
                    )
                    controller.translator._refresh_entry_stats(entry_id)
                self._send_json({"ok": True})
                return

            if path == "/api/export":
                res = controller.packager.export_all(
                    pack_name=body.get("pack_name", "MC-Korean-Translation-Pack"),
                    modpack_name=body.get("modpack_name"),
                    modpack_version=body.get("modpack_version"),
                    create_zip=bool(body.get("create_zip", True)),
                    create_folder=bool(body.get("create_folder", False)),
                    export_ftbquests=bool(body.get("export_ftbquests", True)),
                    export_kubejs=bool(body.get("export_kubejs", True)),
                    auto_enable_options=bool(body.get("auto_enable_options", True)),
                )
                self._send_json(res)
                return

            if path == "/api/export/standalone":
                res = controller.packager.export_standalone(
                    pack_name=body.get("pack_name", "MC-Korean-Translation-Pack"),
                    modpack_name=body.get("modpack_name"),
                    modpack_version=body.get("modpack_version"),
                    export_dir=body.get("export_dir", ""),
                    separate_by_modpack_version=bool(body.get("separate_by_modpack_version", True)),
                    include_resourcepack=bool(body.get("include_resourcepack", True)),
                    include_override=bool(body.get("include_override", True)),
                    include_ftbquests=bool(body.get("include_ftbquests", True)),
                    include_kubejs=bool(body.get("include_kubejs", True)),
                    include_full_bundle=bool(body.get("include_full_bundle", True)),
                    include_mod_archive=bool(body.get("include_mod_archive", True)),
                )
                res["export_history"] = controller.packager.list_exported_history(body.get("export_dir", ""))
                self._send_json(res)
                return

            if path == "/api/archive/import":
                try:
                    archive_data = body.get("archive_data")
                    if isinstance(archive_data, str):
                        archive_data = json.loads(archive_data)
                    imp_res = controller.cache.import_portable_archive_dict(
                        archive_data or {}, overwrite=bool(body.get("overwrite", True))
                    )
                    state = controller.rescan()
                    self._send_json({"ok": True, "imported": imp_res, "state": state})
                except Exception as e:
                    self._send_json({"error": str(e)}, 400)
                return

            self._send_json({"error": "Unknown endpoint"}, 404)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Minecraft Modpack Translator Studio (AGY & Multi-LLM)")
    parser.add_argument(
        "--modpack",
        default=os.path.join(WORKSPACE_DIR, "FTB Evolution 2"),
        help="Path to the Minecraft modpack directory",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for Web Studio UI")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    parser.add_argument(
        "--cli-translate",
        nargs="*",
        help="Optional CLI mode: pass entry_ids (e.g. mod:waystones ftbquests:chapters/welcome.json5) to translate and export directly",
    )
    parser.add_argument("--provider", default="agy", help="Provider ID for CLI mode (agy, gemini_flash, openai_mini, etc.)")
    parser.add_argument("--model", default="gemini-3.8-flash-medium", help="Model name for CLI mode")
    parser.add_argument("--chunk-size", type=int, default=300, help="Chunk size (keys per request)")
    args = parser.parse_args()

    controller = AppController(default_modpack_path=args.modpack)

    if args.cli_translate is not None and len(args.cli_translate) > 0:
        print(f"[CLI 모드] 선택 항목 번역 시작: {args.cli_translate}")
        controller.translator.translate_entries(
            entry_ids=args.cli_translate,
            provider_id=args.provider,
            model=args.model,
            chunk_size=args.chunk_size,
            progress_callback=lambda ev: print(ev["message"]),
        )
        exp = controller.packager.export_all()
        print(f"[패키징 완료] 생성 경로: {exp['outputs']}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), create_request_handler(controller))
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 68)
    print(f"  Minecraft Modpack Translator Studio 실행됨!")
    print(f"  웹 대시보드 주소: {url}")
    print(f"  대상 모드팩 경로: {controller.modpack_path}")
    print("=" * 68)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        server.server_close()


if __name__ == "__main__":
    main()
