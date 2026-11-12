"""Resource Pack Builder & Standalone Exporter (Direct Modpack Apply + Standalone Export Bundles)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from .cache import TranslationCache
from .scanner import (
    ModpackScanner,
    reconstruct_guideme_markdown,
    reconstruct_patchouli_json,
    reconstruct_snbt_quest_text,
    serialize_snbt_lang,
    unflatten_to_original_shape,
)


class ModpackPackager:
    """
    Packages version-verified translations into:
    1. Direct Modpack Apply (`minecraft/resourcepacks/`, `minecraft/config/ftbquests/quests/lang/`, `kubejs/`)
    2. Standalone Export Bundles organized by Modpack & Version:
       `exports/<Modpack_Name>/<Version>/<Pack_Prefix>_Full_Bundle.zip`, `_ResourcePack.zip`, `_FTBQuests_ko_kr.zip`, `_Mod_Archive.json`
    """

    DEFAULT_GENERIC_PACK_NAME = "MC-Korean-Translation-Pack"

    def __init__(self, scanner: ModpackScanner, cache: TranslationCache, default_export_dir: Optional[str] = None):
        self.scanner = scanner
        self.cache = cache
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.default_export_dir = default_export_dir or os.path.join(base_dir, "exports")
        os.makedirs(self.default_export_dir, exist_ok=True)

    @staticmethod
    def _sanitize_fs_segment(text: str, fallback: str = "Unknown") -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (text or "").strip()).strip(" .")
        return cleaned or fallback

    def resolve_export_identity(
        self,
        pack_name: Optional[str] = None,
        modpack_name: Optional[str] = None,
        modpack_version: Optional[str] = None,
    ) -> dict:
        """
        Resolve sanitized Modpack Name, Version tag, and effective ResourcePack / Bundle prefix.
        Prevents exports from different modpacks or different versions from overwriting each other.
        """
        info = getattr(self.scanner, "modpack_info", None) or self.scanner.detect_modpack_metadata()
        raw_mp_name = (modpack_name or "").strip() or info.get("safe_name") or info.get("name") or "Minecraft_Modpack"
        raw_mp_ver = (modpack_version or "").strip() or info.get("version") or "latest"

        safe_mp_name = self._sanitize_fs_segment(raw_mp_name, "Minecraft_Modpack")
        safe_ver_raw = self._sanitize_fs_segment(raw_mp_ver, "latest")
        if safe_ver_raw.lower() == "latest":
            safe_mp_ver = "latest"
        elif safe_ver_raw.lower().startswith("v"):
            safe_mp_ver = safe_ver_raw
        else:
            safe_mp_ver = f"v{safe_ver_raw}"

        mp_slug = re.sub(r"\s+", "_", safe_mp_name)
        auto_pack_title = (
            f"{mp_slug}_{safe_mp_ver}_한글패치" if safe_mp_ver != "latest" else f"{mp_slug}_한글패치"
        )

        req_pack = (pack_name or "").strip()
        if not req_pack or req_pack == self.DEFAULT_GENERIC_PACK_NAME:
            effective_pack_name = auto_pack_title
        else:
            effective_pack_name = self._sanitize_fs_segment(req_pack, auto_pack_title)

        mc_ver = info.get("mc_version") or ""
        desc_ver = f"{safe_mp_ver}" + (f" / MC {mc_ver}" if mc_ver else "")
        description = f"§b[한글 패치] §f{safe_mp_name} ({desc_ver}) 한국어 번역 팩"

        return {
            "modpack_name": safe_mp_name,
            "modpack_version": safe_mp_ver,
            "mc_version": mc_ver,
            "pack_format": self.resolve_pack_format(mc_ver),
            "effective_pack_name": effective_pack_name,
            "auto_pack_title": auto_pack_title,
            "description": description,
        }

    @staticmethod
    def resolve_pack_format(mc_version: str = "") -> int:
        """
        Map Minecraft version string to its exact vanilla resource pack `pack_format` integer.
        Prevents 'Resource pack was made for a newer/older version' warnings across MC 1.12 ~ 26.1+.
        """
        v = (mc_version or "").strip()
        if not v:
            return 15  # Safe default for MC 1.20.1 if unknown
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", v)
        if not m:
            return 15
        major = int(m.group(1))
        minor = int(m.group(2))
        patch = int(m.group(3) or 0)

        if major >= 26:
            return 84
        if major == 1:
            if minor <= 12:
                return 3
            if minor in (13, 14):
                return 4
            if minor == 15:
                return 5
            if minor == 16:
                return 5 if patch <= 1 else 6
            if minor == 17:
                return 7
            if minor == 18:
                return 8
            if minor == 19:
                if patch <= 2:
                    return 9
                if patch == 3:
                    return 12
                return 13
            if minor == 20:
                if patch <= 1:
                    return 15
                if patch == 2:
                    return 18
                if patch in (3, 4):
                    return 22
                return 32
            if minor == 21:
                if patch <= 1:
                    return 34
                if patch in (2, 3):
                    return 42
                if patch == 4:
                    return 46
                if patch == 5:
                    return 55
                return 63
            if minor > 21:
                return 63
        return 15

    @classmethod
    def build_pack_mcmeta(
        cls,
        description: str = "§b[한글 패치] §fAGY 모드팩 한국어 번역 리소스팩",
        mc_version: str = "",
    ) -> dict:
        """Build version-accurate pack.mcmeta tailored to the detected Minecraft version."""
        pack_fmt = cls.resolve_pack_format(mc_version)
        if pack_fmt < 18:
            # MC 1.20.1 and older only read integer `pack_format` (do not include 1.20.2+ fields)
            return {
                "pack": {
                    "pack_format": pack_fmt,
                    "description": description,
                }
            }
        if pack_fmt < 63:
            # MC 1.20.2 ~ 1.21.5 support `pack_format` + `supported_formats`
            return {
                "pack": {
                    "pack_format": pack_fmt,
                    "supported_formats": [15, 99],
                    "description": description,
                }
            }
        # MC 1.21.6+ / 26.1+ support `min_format` and `max_format`
        return {
            "pack": {
                "pack_format": pack_fmt,
                "supported_formats": [15, 999],
                "min_format": [15, 0],
                "max_format": [999, 2147483647],
                "description": description,
            }
        }

    _BROKEN_POS_FMT_RE = re.compile(
        r"%(\d+)\s*\$+(?![-#+ 0,(\<]*\d*(?:\.\d+)?[bBhHsScCdoxXeEfgGaAtT%n])"
    )
    _SPACED_POS_FMT_RE = re.compile(r"%(\d+)\s+\$([bBhHsScCdoxXeEfgGaAtT])")

    @classmethod
    def _repair_format_specifiers(cls, en_val: str, ko_val: str) -> str:
        """
        Repair corrupted Java format specifiers such as `%1$이(가)` -> `%1$s이(가)` or `%1 $s` -> `%1$s`
        that would otherwise cause `UnknownFormatConversionException` in Minecraft.
        """
        if not ko_val or "%" not in ko_val:
            return ko_val
        fixed = cls._SPACED_POS_FMT_RE.sub(r"%\1$\2", ko_val)
        if "$" in fixed:
            def _repl_missing_conv(m: re.Match) -> str:
                idx = m.group(1)
                # Check if en_val had a specific conversion char for this positional index
                en_m = re.search(rf"%{idx}\$[-#+ 0,(\<]*\d*(?:\.\d+)?([bBhHsScCdoxXeEfgGaAtT])", en_val or "")
                conv = en_m.group(1) if en_m else "s"
                return f"%{idx}${conv}"

            fixed = cls._BROKEN_POS_FMT_RE.sub(_repl_missing_conv, fixed)
        return fixed

    @staticmethod
    def _auto_enable_in_options_txt(
        mc_root: str,
        pack_zip_filename: str,
        remove_pack_names: Optional[List[str]] = None,
        set_korean_lang: bool = True,
    ) -> bool:
        """
        Register `file/<pack_zip_filename>` in `options.txt` (`resourcePacks`), remove it from
        `incompatibleResourcePacks`, and set `lang:ko_kr` so the pack loads on startup without
        requiring a crash-prone in-game full shader/atlas reload (especially on ATM9 / LDLib).
        """
        options_path = os.path.join(mc_root, "options.txt")
        if not os.path.isfile(options_path):
            return False

        target_entry = f"file/{pack_zip_filename}"
        stale_entries = {target_entry}
        for name in (remove_pack_names or []):
            if name:
                stale_entries.add(f"file/{name}")

        try:
            with open(options_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            new_lines: List[str] = []
            found_rp = False
            found_incompat = False
            found_lang = False

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("resourcePacks:"):
                    found_rp = True
                    raw_val = stripped.split(":", 1)[1].strip()
                    try:
                        rp_list = json.loads(raw_val)
                        if not isinstance(rp_list, list):
                            rp_list = ["vanilla", "mod_resources"]
                    except Exception:
                        rp_list = ["vanilla", "mod_resources"]
                    rp_list = [
                        x for x in rp_list
                        if x not in stale_entries and not (isinstance(x, str) and x.endswith("_한글패치_ResourcePack.zip"))
                    ]
                    rp_list.append(target_entry)
                    new_lines.append(f"resourcePacks:{json.dumps(rp_list, ensure_ascii=False)}\n")
                elif stripped.startswith("incompatibleResourcePacks:"):
                    found_incompat = True
                    raw_val = stripped.split(":", 1)[1].strip()
                    try:
                        inc_list = json.loads(raw_val)
                        if not isinstance(inc_list, list):
                            inc_list = []
                    except Exception:
                        inc_list = []
                    inc_list = [
                        x for x in inc_list
                        if x not in stale_entries and not (isinstance(x, str) and "한글패치" in x)
                    ]
                    new_lines.append(f"incompatibleResourcePacks:{json.dumps(inc_list, ensure_ascii=False)}\n")
                elif stripped.startswith("lang:") and set_korean_lang:
                    found_lang = True
                    new_lines.append("lang:ko_kr\n")
                else:
                    new_lines.append(line)

            if not found_rp:
                new_lines.append(f'resourcePacks:["vanilla","mod_resources",{json.dumps(target_entry, ensure_ascii=False)}]\n')
            if not found_incompat:
                new_lines.append("incompatibleResourcePacks:[]\n")
            if not found_lang and set_korean_lang:
                new_lines.append("lang:ko_kr\n")

            with open(options_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            return True
        except Exception:
            return False

    @staticmethod
    def _safe_write_zip(target_path: str, populate_fn) -> str:
        """
        Write a ZIP archive to a temporary file first and replace `target_path`.
        If `target_path` is locked by another process on Windows (e.g., open in Explorer or an archive viewer),
        falls back to `<stem>_updated.zip` instead of failing with PermissionError.
        """
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        tmp_path = f"{target_path}.tmp.{os.getpid()}"
        try:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                populate_fn(zf)
            for attempt in range(3):
                try:
                    os.replace(tmp_path, target_path)
                    return target_path
                except PermissionError:
                    try:
                        os.chmod(target_path, 0o666)
                        os.remove(target_path)
                        os.replace(tmp_path, target_path)
                        return target_path
                    except Exception:
                        time.sleep(0.15 * (attempt + 1))
            # If still locked by an external process, keep as `<stem>_updated.zip`
            base, ext = os.path.splitext(target_path)
            alt_path = f"{base}_updated{ext}"
            os.replace(tmp_path, alt_path)
            return alt_path
        finally:
            if os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    @staticmethod
    def _serialize_quest_file_bytes(rel_lang_path: str, reconstructed: object) -> bytes:
        """Serialize reconstructed FTB Quests data as direct SNBT bytes/str, SNBT lang (`.snbt`), or JSON (`.json5`/`.json`)."""
        if isinstance(reconstructed, (bytes, bytearray)):
            return bytes(reconstructed)
        if isinstance(reconstructed, str):
            return reconstructed.encode("utf-8")
        if isinstance(reconstructed, dict) and rel_lang_path.lower().endswith(".snbt"):
            return serialize_snbt_lang(reconstructed).encode("utf-8")
        return json.dumps(reconstructed, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _build_guideme_ko_asset_path(ns: str, rel_key: str) -> str:
        """
        Convert a GuideMe or Oracle Index relative key (`<guide_root>/<rel_md>`) into its localized
        ResourcePack asset path (`assets/<ns>/<guide_root>/_ko_kr/<rel_md>`).
        Supports:
        - `__oracle__/<book_id>/<rest>` -> `assets/oracle_index/books/<book_id>/translated/ko_kr/<rest>`
        - `guides/<g_ns>/<g_name>/<rel>.md` -> `assets/<ns>/guides/<g_ns>/<g_name>/_ko_kr/<rel>.md`
        - `<folder>/<rel>.md` (`ae2guide`, `mi_guidebook`, `nodeworksguide`, `ftbic`, `auroral`, etc.)
          -> `assets/<ns>/<folder>/_ko_kr/<rel>.md`
        """
        parts = rel_key.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "__oracle__":
            book_id = parts[1]
            rest = "/".join(parts[2:])
            return f"assets/oracle_index/books/{book_id}/translated/ko_kr/{rest}"
        if len(parts) >= 4 and parts[0].lower() == "guides":
            g_root = "/".join(parts[:3])
            rel_md = "/".join(parts[3:])
        else:
            g_root = parts[0]
            rel_md = "/".join(parts[1:])
        return f"assets/{ns}/{g_root}/_ko_kr/{rel_md}"

    def _collect_version_safe_translations(
        self, only_entry_ids: Optional[List[str]] = None
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, object], Dict[str, Dict[str, str]], Dict[str, bytes], int]:
        """
        Assemble version-verified translations ONLY for entries present in the currently scanned modpack:
        - Reuses saved translations where `saved_en == current_en`
        - Reuses in-mod translations when a key was renamed in a new mod version (`en_val in mod_en_to_ko`)
        - Avoids applying outdated translations or quests from a different modpack
        Returns: `(mod_lang_files, ftbquest_reconstructed_files_by_rel_lang_path, kubejs_lang_files, extra_rp_assets, total_exported_keys)`
        """
        all_cached_with_en = self.cache.get_all_entry_translations_with_en()
        # CRITICAL: Only export entries that actually exist in the currently scanned modpack!
        current_modpack_ids = set(self.scanner.entries.keys())
        target_ids = (set(only_entry_ids) & current_modpack_ids) if only_entry_ids else current_modpack_ids

        # Build a shared lookup across this modpack's KubeJS and FTBQuests entries so `{key}` references
        # (e.g., `moni.quest.*` in `kubejs:ftbquests` or `atm9.quest.*` in `kubejs:kubejs`) are shared seamlessly
        shared_quest_kubejs_cache: Dict[str, Tuple[str, str]] = {}
        shared_quest_en_to_ko: Dict[str, str] = {}
        kubejs_ns_by_key: Dict[str, str] = {}
        for eid in sorted(current_modpack_ids):
            ent = self.scanner.entries.get(eid)
            if not ent or ent.source_type not in ("kubejs", "ftbquests"):
                continue
            if ent.source_type == "kubejs":
                for k_kj in self.scanner.get_en_dict(eid):
                    kubejs_ns_by_key[k_kj] = ent.mod_id
            c_map = dict(self.scanner.get_linked_cache_dict(eid))
            c_map.update(all_cached_with_en.get(eid, {}))
            for k_c, c_val in c_map.items():
                if isinstance(c_val, tuple) and len(c_val) == 2:
                    s_en, s_ko = c_val
                    if s_ko and s_ko.strip():
                        shared_quest_kubejs_cache[k_c] = (s_en, s_ko)
                        if s_en:
                            shared_quest_en_to_ko[s_en] = s_ko

        mod_lang_files: Dict[str, Dict[str, str]] = {}
        ftbquest_files: Dict[str, object] = {}
        kubejs_lang_files: Dict[str, Dict[str, str]] = {}
        extra_rp_assets: Dict[str, bytes] = {}
        total_exported_keys = 0

        # Process mod_jar first (0), then kubejs (1), then ftbquests/guideme/patchouli (2)
        sorted_ids = sorted(
            target_ids,
            key=lambda eid: (
                0
                if self.scanner.entries[eid].source_type == "mod_jar"
                else (1 if self.scanner.entries[eid].source_type == "kubejs" else 2),
                eid,
            ),
        )

        for entry_id in sorted_ids:
            entry = self.scanner.entries.get(entry_id)
            if not entry:
                continue

            en_flat = self.scanner.get_en_dict(entry_id)
            if not en_flat:
                continue

            builtin_ko = self.scanner.get_builtin_ko_dict(entry_id)
            cached_with_en = dict(self.scanner.get_linked_cache_dict(entry_id))
            if entry.source_type in ("kubejs", "ftbquests"):
                for k_sh, v_sh in shared_quest_kubejs_cache.items():
                    if k_sh in en_flat:
                        cached_with_en.setdefault(k_sh, v_sh)
            cached_with_en.update(all_cached_with_en.get(entry_id, {}))

            mod_en_to_ko: Dict[str, str] = (
                dict(shared_quest_en_to_ko) if entry.source_type in ("kubejs", "ftbquests") else {}
            )
            for s_en, s_ko in cached_with_en.values():
                if s_en and s_ko and s_ko.strip():
                    mod_en_to_ko[s_en] = s_ko

            merged_ko: Dict[str, str] = {
                k: self._repair_format_specifiers(en_flat.get(k, ""), v)
                for k, v in builtin_ko.items()
            }
            # Version-aware assembly against the current modpack's en_flat
            for k, en_val in en_flat.items():
                if k in cached_with_en:
                    saved_en, saved_ko = cached_with_en[k]
                    if saved_ko and saved_ko.strip() and (
                        saved_en == en_val or (not saved_en and entry.source_type != "ftbquests")
                    ):
                        merged_ko[k] = self._repair_format_specifiers(en_val, saved_ko)
                    elif en_val in mod_en_to_ko:
                        merged_ko[k] = self._repair_format_specifiers(en_val, mod_en_to_ko[en_val])
                elif en_val in mod_en_to_ko:
                    merged_ko[k] = self._repair_format_specifiers(en_val, mod_en_to_ko[en_val])

            if not merged_ko:
                continue

            if entry.source_type == "mod_jar":
                ns = entry.mod_id
                # Include `en_flat` fallback before `merged_ko` so our ResourcePack's `ko_kr.json`
                # overrides any stale/broken built-in `ko_kr.json` inside the Mod JAR (e.g., Occultism's
                # outdated Crowdin `ko_kr.json` with broken `entry://` links).
                ns_target = mod_lang_files.setdefault(ns, {})
                for k_en, v_en in en_flat.items():
                    ns_target.setdefault(k_en, v_en)
                ns_target.update(merged_ko)
                total_exported_keys += len(merged_ko)

            elif entry.source_type == "guideme":
                ns = entry.mod_id
                orig_md_files = self.scanner.get_raw_original_data(entry_id) or {}
                files_with_ko = {k.split("::", 1)[0] for k in merged_ko.keys() if "::" in k}
                for rel_key in sorted(files_with_ko):
                    orig_md = orig_md_files.get(rel_key)
                    if not orig_md:
                        continue
                    asset_path = self._build_guideme_ko_asset_path(ns, rel_key)
                    if rel_key.lower().endswith(".json") and isinstance(orig_md, dict):
                        reconstructed_meta = {
                            k: (merged_ko.get(f"{rel_key}::{k}") or v)
                            for k, v in orig_md.items()
                        }
                        extra_rp_assets[asset_path] = json.dumps(
                            reconstructed_meta, ensure_ascii=False, indent=2
                        ).encode("utf-8")
                    elif isinstance(orig_md, str):
                        reconstructed_md = reconstruct_guideme_markdown(orig_md, merged_ko, file_prefix=rel_key)
                        extra_rp_assets[asset_path] = reconstructed_md.encode("utf-8")
                total_exported_keys += len(merged_ko)

            elif entry.source_type == "patchouli":
                # entry_id is "patchouli:<ns>/<book_name>"
                ns_book = entry.entry_id.split(":", 1)[1] if ":" in entry.entry_id else f"{entry.mod_id}/book"
                if "/" in ns_book:
                    pt_ns, pt_book = ns_book.split("/", 1)
                else:
                    pt_ns, pt_book = entry.mod_id, "book"
                orig_json_files = self.scanner.get_raw_original_data(entry_id) or {}
                files_with_ko = {k.split("::", 1)[0] for k in merged_ko.keys() if "::" in k}
                for rel_json in sorted(files_with_ko):
                    orig_obj = orig_json_files.get(rel_json)
                    if not orig_obj or not isinstance(orig_obj, dict):
                        continue
                    reconstructed_obj = reconstruct_patchouli_json(orig_obj, merged_ko, file_prefix=rel_json)
                    asset_path = f"assets/{pt_ns}/patchouli_books/{pt_book}/ko_kr/{rel_json}"
                    extra_rp_assets[asset_path] = json.dumps(
                        reconstructed_obj, ensure_ascii=False, indent=2
                    ).encode("utf-8")
                total_exported_keys += len(merged_ko)

            elif entry.source_type == "ftbquests":
                orig_raw = self.scanner.get_raw_original_data(entry_id)
                if isinstance(orig_raw, dict) and orig_raw.get("__snbt_direct__"):
                    rel_quest_path = orig_raw.get("rel_quest_path", "")
                    orig_text = orig_raw.get("orig_text", "")
                    en_to_ko_inline = {
                        en_flat[k]: v for k, v in merged_ko.items() if k in en_flat and v and v.strip()
                    }
                    en_to_ko_inline.update(mod_en_to_ko)
                    if rel_quest_path and orig_text:
                        reconstructed_snbt = reconstruct_snbt_quest_text(
                            orig_text, merged_ko, en_to_ko_fallback=en_to_ko_inline
                        )
                        ftbquest_files[f"__direct__/{rel_quest_path}"] = reconstructed_snbt.encode("utf-8")

                    for ov in orig_raw.get("overrides", []):
                        ov_rel = ov.get("rel_mc_path", "")
                        ov_orig = ov.get("orig_text", "")
                        ov_prefix = ov.get("file_prefix", "")
                        if ov_rel and ov_orig:
                            ov_snbt = reconstruct_snbt_quest_text(
                                ov_orig,
                                merged_ko,
                                en_to_ko_fallback=en_to_ko_inline,
                                file_prefix=ov_prefix,
                            )
                            ftbquest_files[f"__direct__/{ov_rel}"] = ov_snbt.encode("utf-8")

                    # Also propagate any `{key}` translations back into KubeJS / ResourcePack lang dicts
                    for k_q, v_q in merged_ko.items():
                        if k_q.startswith("__inline__"):
                            continue
                        kj_ns = kubejs_ns_by_key.get(k_q)
                        if kj_ns:
                            mod_lang_files.setdefault(kj_ns, {})[k_q] = v_q
                            kubejs_lang_files.setdefault(kj_ns, {})[k_q] = v_q
                    total_exported_keys += len(merged_ko)
                else:
                    src_rel = entry.en_path_in_source
                    if src_rel.startswith("__single__/"):
                        en_fname = src_rel.split("/", 1)[1]
                        _, ext = os.path.splitext(en_fname)
                        rel_lang_path = f"ko_kr{ext}"
                    else:
                        rel_lang_path = f"ko_kr/{src_rel}"

                    reconstructed = (
                        unflatten_to_original_shape(orig_raw, merged_ko, fallback_en=True)
                        if orig_raw
                        else merged_ko
                    )
                    ftbquest_files[rel_lang_path] = reconstructed
                    total_exported_keys += len(merged_ko)

            elif entry.source_type == "kubejs":
                ns = entry.mod_id
                ns_target = mod_lang_files.setdefault(ns, {})
                for k_en, v_en in en_flat.items():
                    ns_target.setdefault(k_en, v_en)
                ns_target.update(merged_ko)
                kj_target = kubejs_lang_files.setdefault(ns, {})
                for k_en, v_en in en_flat.items():
                    kj_target.setdefault(k_en, v_en)
                kj_target.update(merged_ko)
                total_exported_keys += len(merged_ko)

        return mod_lang_files, ftbquest_files, kubejs_lang_files, extra_rp_assets, total_exported_keys

    def export_all(
        self,
        pack_name: str = "MC-Korean-Translation-Pack",
        modpack_name: Optional[str] = None,
        modpack_version: Optional[str] = None,
        create_zip: bool = True,
        create_folder: bool = False,
        export_ftbquests: bool = True,
        export_kubejs: bool = True,
        auto_enable_options: bool = True,
        only_entry_ids: Optional[List[str]] = None,
    ) -> dict:
        """
        Directly apply translated files into the modpack's `resourcepacks/`, `config/ftbquests`, and `kubejs`.
        Uses modpack-and-version-specific resourcepack naming and MC-version-accurate `pack_format` by default.
        """
        identity = self.resolve_export_identity(pack_name, modpack_name, modpack_version)
        effective_pack_name = identity["effective_pack_name"]

        mc_root = self.scanner.mc_root
        resourcepacks_dir = os.path.join(mc_root, "resourcepacks")
        os.makedirs(resourcepacks_dir, exist_ok=True)

        mod_lang_files, ftbquest_files, kubejs_lang_files, extra_rp_assets, total_exported_keys = (
            self._collect_version_safe_translations(only_entry_ids)
        )

        outputs: List[str] = []
        ftbquest_files_written = 0
        kubejs_files_written = 0
        mcmeta_dict = self.build_pack_mcmeta(identity["description"], identity["mc_version"])
        mcmeta_str = json.dumps(mcmeta_dict, ensure_ascii=False, indent=2)
        has_rp_content = bool(mod_lang_files or extra_rp_assets)

        # 1. Create Resource Pack ZIP in modpack/resourcepacks/
        zip_filename = f"{effective_pack_name}.zip"
        if create_zip and has_rp_content:
            zip_path = os.path.join(resourcepacks_dir, zip_filename)

            def _write_direct_rp(zf: zipfile.ZipFile) -> None:
                zf.writestr("pack.mcmeta", mcmeta_str.encode("utf-8"))
                for ns, ko_dict in sorted(mod_lang_files.items()):
                    json_bytes = json.dumps(ko_dict, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(f"assets/{ns}/lang/ko_kr.json", json_bytes)
                for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                    zf.writestr(asset_rel_path, asset_bytes)

            zip_path = self._safe_write_zip(zip_path, _write_direct_rp)
            zip_filename = os.path.basename(zip_path)
            outputs.append(zip_path)

            # Remove duplicate `_ResourcePack.zip` or legacy generic `MC-Korean-Translation-Pack` in `resourcepacks/`
            for stale_rp in (
                f"{effective_pack_name}_ResourcePack.zip",
                f"{self.DEFAULT_GENERIC_PACK_NAME}.zip"
                if effective_pack_name != self.DEFAULT_GENERIC_PACK_NAME
                else "",
            ):
                if stale_rp:
                    stale_rp_p = os.path.join(resourcepacks_dir, stale_rp)
                    if os.path.isfile(stale_rp_p):
                        try:
                            os.remove(stale_rp_p)
                        except Exception:
                            pass
            if effective_pack_name != self.DEFAULT_GENERIC_PACK_NAME:
                stale_generic_dir = os.path.join(resourcepacks_dir, self.DEFAULT_GENERIC_PACK_NAME)
                if os.path.isdir(stale_generic_dir):
                    try:
                        shutil.rmtree(stale_generic_dir)
                    except Exception:
                        pass

        # 2. Create Resource Pack Folder in modpack/resourcepacks/ (or clean up duplicate folder if unchecked)
        folder_path = os.path.join(resourcepacks_dir, effective_pack_name)
        if create_folder and has_rp_content:
            os.makedirs(folder_path, exist_ok=True)
            with open(os.path.join(folder_path, "pack.mcmeta"), "w", encoding="utf-8") as f:
                f.write(mcmeta_str)
            for ns, ko_dict in sorted(mod_lang_files.items()):
                lang_dir = os.path.join(folder_path, "assets", ns, "lang")
                os.makedirs(lang_dir, exist_ok=True)
                with open(os.path.join(lang_dir, "ko_kr.json"), "w", encoding="utf-8") as f:
                    json.dump(ko_dict, f, ensure_ascii=False, indent=2)
            for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                full_asset_p = os.path.join(folder_path, asset_rel_path.replace("/", os.sep))
                os.makedirs(os.path.dirname(full_asset_p), exist_ok=True)
                with open(full_asset_p, "wb") as f:
                    f.write(asset_bytes)
            outputs.append(folder_path)
        elif create_zip and os.path.isdir(folder_path):
            # Avoid keeping both `<pack>.zip` and `<pack>/` folder in `resourcepacks/`
            try:
                shutil.rmtree(folder_path)
            except Exception:
                pass

        # Auto-enable resource pack & `lang:ko_kr` in `options.txt` so it loads at boot without in-game reload crash
        options_updated = False
        if auto_enable_options and (create_zip or create_folder) and has_rp_content:
            active_pack_ref = zip_filename if create_zip else effective_pack_name
            options_updated = self._auto_enable_in_options_txt(
                mc_root=mc_root,
                pack_zip_filename=active_pack_ref,
                remove_pack_names=[
                    effective_pack_name,
                    zip_filename,
                    f"{effective_pack_name}_ResourcePack.zip",
                    f"{self.DEFAULT_GENERIC_PACK_NAME}.zip",
                    self.DEFAULT_GENERIC_PACK_NAME,
                ],
                set_korean_lang=True,
            )
            if options_updated:
                outputs.append(os.path.join(mc_root, "options.txt"))

        # 3. Direct FTB Quests injection into modpack/config/ftbquests/quests/ (and config-overrides/)
        if export_ftbquests and ftbquest_files:
            ftb_quests_base = os.path.join(mc_root, "config", "ftbquests", "quests")
            ftb_lang_base = os.path.join(ftb_quests_base, "lang")
            if not os.path.isdir(os.path.join(ftb_lang_base, "en_us")) and os.path.isdir(
                os.path.join(ftb_lang_base, "ko_kr")
            ):
                try:
                    shutil.rmtree(os.path.join(ftb_lang_base, "ko_kr"))
                except Exception:
                    pass

            for rel_lang_path, reconstructed in sorted(ftbquest_files.items()):
                if rel_lang_path.startswith("__direct__/"):
                    rel_mc_path = rel_lang_path[len("__direct__/") :]
                    ftb_ko_target = os.path.join(mc_root, rel_mc_path.replace("/", os.sep))
                    self.scanner.ensure_snbt_quest_backup(ftb_ko_target)
                else:
                    ftb_ko_target = os.path.join(ftb_lang_base, rel_lang_path.replace("/", os.sep))
                os.makedirs(os.path.dirname(ftb_ko_target), exist_ok=True)
                file_bytes = self._serialize_quest_file_bytes(rel_lang_path, reconstructed)
                with open(ftb_ko_target, "wb") as f:
                    f.write(file_bytes)
                ftbquest_files_written += 1
            outputs.append(ftb_quests_base)

        # 4. Direct KubeJS injection into modpack/kubejs/assets/ (lang + GuideMe/Patchouli/Oracle assets)
        kubejs_target_map = kubejs_lang_files if kubejs_lang_files else mod_lang_files
        if export_kubejs and (kubejs_target_map or extra_rp_assets) and os.path.isdir(os.path.join(mc_root, "kubejs")):
            kubejs_assets = os.path.join(mc_root, "kubejs", "assets")
            # Clean up any leftover foreign ko_kr.json from other modpacks that don't exist in this modpack
            if os.path.isdir(kubejs_assets):
                for existing_ns in os.listdir(kubejs_assets):
                    ns_dir = os.path.join(kubejs_assets, existing_ns)
                    ko_p = os.path.join(ns_dir, "lang", "ko_kr.json")
                    en_p = os.path.join(ns_dir, "lang", "en_us.json")
                    if os.path.isfile(ko_p) and existing_ns not in mod_lang_files and not os.path.isfile(en_p):
                        try:
                            os.remove(ko_p)
                        except Exception:
                            pass

            for ns, ko_dict in sorted(kubejs_target_map.items()):
                lang_dir = os.path.join(kubejs_assets, ns, "lang")
                os.makedirs(lang_dir, exist_ok=True)
                with open(os.path.join(lang_dir, "ko_kr.json"), "w", encoding="utf-8") as f:
                    json.dump(ko_dict, f, ensure_ascii=False, indent=2)
                kubejs_files_written += 1

            # Also write GuideMe / Patchouli / Oracle Index localized assets into kubejs/assets/
            # so KubeJS File Resource Pack [assets] (which loads at highest priority) serves them directly.
            for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                full_kj_asset = os.path.join(mc_root, "kubejs", asset_rel_path.replace("/", os.sep))
                os.makedirs(os.path.dirname(full_kj_asset), exist_ok=True)
                with open(full_kj_asset, "wb") as f:
                    f.write(asset_bytes)
                kubejs_files_written += 1
            outputs.append(kubejs_assets)

        return {
            "mode": "direct_apply",
            "modpack_name": identity["modpack_name"],
            "modpack_version": identity["modpack_version"],
            "mc_version": identity["mc_version"],
            "pack_format": identity["pack_format"],
            "pack_name": effective_pack_name,
            "options_txt_updated": options_updated,
            "mod_namespaces_count": len(mod_lang_files),
            "guidebook_files_written": len(extra_rp_assets),
            "ftbquest_files_written": ftbquest_files_written,
            "kubejs_files_written": kubejs_files_written,
            "total_exported_keys": total_exported_keys,
            "outputs": outputs,
        }

    def export_standalone(
        self,
        pack_name: str = "MC-Korean-Translation-Pack",
        modpack_name: Optional[str] = None,
        modpack_version: Optional[str] = None,
        export_dir: Optional[str] = None,
        separate_by_modpack_version: bool = True,
        include_resourcepack: bool = True,
        include_override: bool = True,
        include_ftbquests: bool = True,
        include_kubejs: bool = True,
        include_full_bundle: bool = True,
        include_mod_archive: bool = True,
        only_entry_ids: Optional[List[str]] = None,
    ) -> dict:
        """
        Export standalone distribution files into `<base_export_dir>/<Modpack_Name>/<Version>/`
        (default base: `mc_translator/exports`) without modifying the modpack folder.
        Bundles FTB Quests (`config/ftbquests/...`) and KubeJS (`kubejs/assets/...`) together into `<prefix>_Override.zip`.
        """
        identity = self.resolve_export_identity(pack_name, modpack_name, modpack_version)
        mp_name = identity["modpack_name"]
        mp_ver = identity["modpack_version"]
        effective_pack_name = identity["effective_pack_name"]

        base_target_dir = (
            os.path.abspath(export_dir.strip())
            if (export_dir and export_dir.strip())
            else os.path.abspath(self.default_export_dir)
        )

        if separate_by_modpack_version:
            norm_base = os.path.normpath(base_target_dir)
            expected_tail = os.path.normpath(os.path.join(mp_name, mp_ver))
            if norm_base.lower().endswith(expected_tail.lower()):
                target_dir = base_target_dir
                base_target_dir = os.path.dirname(os.path.dirname(norm_base))
            else:
                target_dir = os.path.join(base_target_dir, mp_name, mp_ver)
        else:
            target_dir = base_target_dir

        os.makedirs(target_dir, exist_ok=True)

        mod_lang_files, ftbquest_files, kubejs_lang_files, extra_rp_assets, total_exported_keys = (
            self._collect_version_safe_translations(only_entry_ids)
        )
        mcmeta_dict = self.build_pack_mcmeta(identity["description"], identity["mc_version"])
        mcmeta_str = json.dumps(mcmeta_dict, ensure_ascii=False, indent=2)
        has_rp_content = bool(mod_lang_files or extra_rp_assets)

        outputs: List[str] = []
        download_files: List[Dict[str, str]] = []

        def _make_download_item(label: str, filename: str, full_path: str) -> Dict[str, str]:
            try:
                rel_p = os.path.relpath(full_path, base_target_dir).replace("\\", "/")
            except ValueError:
                rel_p = filename
            size_kb = round(os.path.getsize(full_path) / 1024, 1) if os.path.isfile(full_path) else 0
            return {
                "label": label,
                "filename": filename,
                "rel_path": rel_p,
                "path": full_path,
                "size_kb": size_kb,
            }

        # 1. Standalone ResourcePack ZIP
        rp_zip_bytes: Optional[bytes] = None
        if (include_resourcepack or include_full_bundle) and has_rp_content:
            rp_filename = f"{effective_pack_name}_ResourcePack.zip"
            rp_path = os.path.join(target_dir, rp_filename)

            def _write_rp_zip(zf: zipfile.ZipFile) -> None:
                zf.writestr("pack.mcmeta", mcmeta_str.encode("utf-8"))
                for ns, ko_dict in sorted(mod_lang_files.items()):
                    json_bytes = json.dumps(ko_dict, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(f"assets/{ns}/lang/ko_kr.json", json_bytes)
                for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                    zf.writestr(asset_rel_path, asset_bytes)

            rp_path = self._safe_write_zip(rp_path, _write_rp_zip)
            rp_filename = os.path.basename(rp_path)
            with open(rp_path, "rb") as f:
                rp_zip_bytes = f.read()
            if include_resourcepack:
                outputs.append(rp_path)
                download_files.append(_make_download_item("🎨 리소스팩 단독 (.zip)", rp_filename, rp_path))

        # 2. Combined Override ZIP (FTB Quests `config/ftbquests/...` + KubeJS `kubejs/assets/...`)
        has_kubejs_dir = os.path.isdir(os.path.join(self.scanner.mc_root, "kubejs"))
        kubejs_export_map: Dict[str, Dict[str, str]] = {}
        if kubejs_lang_files:
            for ns in kubejs_lang_files:
                kubejs_export_map[ns] = mod_lang_files.get(ns, kubejs_lang_files[ns])
        elif has_kubejs_dir and mod_lang_files:
            kubejs_export_map = mod_lang_files

        # Clean up any legacy separate `_FTBQuests_ko_kr.zip` / `_KubeJS_ko_kr.zip` in `target_dir`
        for old_suffix in ("_FTBQuests_ko_kr.zip", "_KubeJS_ko_kr.zip"):
            old_p = os.path.join(target_dir, f"{effective_pack_name}{old_suffix}")
            if os.path.isfile(old_p):
                try:
                    os.remove(old_p)
                except Exception:
                    pass

        do_override = include_override and (include_ftbquests or include_kubejs)
        if do_override and (ftbquest_files or kubejs_export_map or extra_rp_assets):
            override_filename = f"{effective_pack_name}_Override.zip"
            override_path = os.path.join(target_dir, override_filename)

            def _write_override_zip(zf: zipfile.ZipFile) -> None:
                if include_ftbquests:
                    for rel_lang_path, reconstructed in sorted(ftbquest_files.items()):
                        file_bytes = self._serialize_quest_file_bytes(rel_lang_path, reconstructed)
                        zip_arc_path = (
                            rel_lang_path[len("__direct__/") :]
                            if rel_lang_path.startswith("__direct__/")
                            else f"config/ftbquests/quests/lang/{rel_lang_path}"
                        )
                        zf.writestr(zip_arc_path, file_bytes)
                if include_kubejs:
                    for ns, ko_dict in sorted(kubejs_export_map.items()):
                        json_bytes = json.dumps(ko_dict, ensure_ascii=False, indent=2).encode("utf-8")
                        zf.writestr(f"kubejs/assets/{ns}/lang/ko_kr.json", json_bytes)
                    for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                        zf.writestr(f"kubejs/{asset_rel_path}", asset_bytes)

            override_path = self._safe_write_zip(override_path, _write_override_zip)
            override_filename = os.path.basename(override_path)
            outputs.append(override_path)
            download_files.append(
                _make_download_item("📂 덮어쓰기용 Override (FTB Quests + KubeJS + 가이드북 .zip)", override_filename, override_path)
            )

        # 3. Full Distribution Bundle ZIP (ResourcePack + Override [FTB Quests + KubeJS + Guidebooks] + README)
        if include_full_bundle and (has_rp_content or ftbquest_files or kubejs_export_map):
            bundle_filename = f"{effective_pack_name}_Full_Bundle.zip"
            bundle_path = os.path.join(target_dir, bundle_filename)
            bundle_kubejs_map = kubejs_export_map if kubejs_lang_files else {}
            readme_txt = (
                f"=== {mp_name} ({mp_ver}) 한글 패치 통합 번들 ===\n"
                f"패키지 이름: {effective_pack_name}\n"
                f"생성 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"포함된 모드 네임스페이스: {len(mod_lang_files)}개 | 인게임 가이드북/책자 파일: {len(extra_rp_assets)}개 | FTB Quests 파일: {len(ftbquest_files)}개 | KubeJS 에셋: {len(kubejs_export_map)}개 (총 {total_exported_keys}키)\n\n"
                f"[설치 방법]\n"
                f"1. 이 압축 파일 안의 폴더(`resourcepacks`, `config`, `kubejs`)를 `{mp_name}` 모드팩 루트(minecraft 폴더)에 그대로 덮어씌우세요.\n"
                f"2. 게임 실행 후 [설정 -> 리소스 팩]에서 `{effective_pack_name}.zip`을 활성화하세요.\n"
                f"3. 멀티플레이 서버 운영 시: 서버의 `config` 및 `kubejs` 폴더에도 본 압축파일(또는 `{effective_pack_name}_Override.zip`)을 덮어씌워야 접속자에게 한글 퀘스트가 동기화됩니다.\n"
            )

            def _write_bundle_zip(zf: zipfile.ZipFile) -> None:
                zf.writestr("README_설치안내.txt", readme_txt.encode("utf-8"))
                if rp_zip_bytes:
                    zf.writestr(f"resourcepacks/{effective_pack_name}.zip", rp_zip_bytes)
                for rel_lang_path, reconstructed in sorted(ftbquest_files.items()):
                    file_bytes = self._serialize_quest_file_bytes(rel_lang_path, reconstructed)
                    zip_arc_path = (
                        rel_lang_path[len("__direct__/") :]
                        if rel_lang_path.startswith("__direct__/")
                        else f"config/ftbquests/quests/lang/{rel_lang_path}"
                    )
                    zf.writestr(zip_arc_path, file_bytes)
                for ns, ko_dict in sorted(bundle_kubejs_map.items()):
                    json_bytes = json.dumps(ko_dict, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(f"kubejs/assets/{ns}/lang/ko_kr.json", json_bytes)
                if include_kubejs:
                    for asset_rel_path, asset_bytes in sorted(extra_rp_assets.items()):
                        zf.writestr(f"kubejs/{asset_rel_path}", asset_bytes)

            bundle_path = self._safe_write_zip(bundle_path, _write_bundle_zip)
            bundle_filename = os.path.basename(bundle_path)
            outputs.append(bundle_path)
            download_files.append(_make_download_item("📦 통합 배포 번들 (리소스팩+Override 합본 .zip)", bundle_filename, bundle_path))

        # 4. Portable Cross-Version Mod Translation Archive JSON (scoped to this modpack's entries)
        if include_mod_archive:
            archive_filename = f"{effective_pack_name}_Mod_Archive.json"
            archive_path = os.path.join(target_dir, archive_filename)
            scoped_ids = list(only_entry_ids) if only_entry_ids else list(self.scanner.entries.keys())
            archive_dict = self.cache.export_portable_archive_dict(only_entry_ids=scoped_ids)
            archive_dict["modpack_name"] = mp_name
            archive_dict["modpack_version"] = mp_ver
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(archive_dict, f, ensure_ascii=False, indent=2)
            outputs.append(archive_path)
            download_files.append(_make_download_item("💾 버전 호환 모드 번역 보관소 백업 (.json)", archive_filename, archive_path))

        # 5. Save export_meta.json inside `<Modpack_Name>/<Version>/` for history browsing
        exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
        meta_payload = {
            "modpack_name": mp_name,
            "modpack_version": mp_ver,
            "mc_version": identity["mc_version"],
            "pack_format": identity["pack_format"],
            "pack_name": effective_pack_name,
            "exported_at": exported_at,
            "timestamp": time.time(),
            "export_dir": target_dir,
            "mod_namespaces_count": len(mod_lang_files),
            "guidebook_files_written": len(extra_rp_assets),
            "ftbquest_files_written": len(ftbquest_files),
            "kubejs_files_written": len(kubejs_export_map),
            "total_exported_keys": total_exported_keys,
            "download_files": download_files,
        }
        try:
            with open(os.path.join(target_dir, "export_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta_payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        return {
            "mode": "standalone_export",
            "modpack_name": mp_name,
            "modpack_version": mp_ver,
            "mc_version": identity["mc_version"],
            "pack_format": identity["pack_format"],
            "pack_name": effective_pack_name,
            "base_export_dir": base_target_dir,
            "export_dir": target_dir,
            "exported_at": exported_at,
            "mod_namespaces_count": len(mod_lang_files),
            "guidebook_files_written": len(extra_rp_assets),
            "ftbquest_files_written": len(ftbquest_files),
            "kubejs_files_written": len(kubejs_export_map),
            "total_exported_keys": total_exported_keys,
            "outputs": outputs,
            "download_files": download_files,
        }

    def list_exported_history(self, base_export_dir: Optional[str] = None) -> List[dict]:
        """
        Scan `exports/<Modpack_Name>/<Version>/` directories and return a sorted history
        of all exported modpacks and versions so the user can view/download any previous export.
        """
        root_dir = (
            os.path.abspath(base_export_dir.strip())
            if (base_export_dir and base_export_dir.strip())
            else os.path.abspath(self.default_export_dir)
        )
        if not os.path.isdir(root_dir):
            return []

        history: List[dict] = []

        # 1. Scan `<Modpack_Name>/<Version>/` directories
        for mp_entry in sorted(os.listdir(root_dir)):
            mp_dir = os.path.join(root_dir, mp_entry)
            if not os.path.isdir(mp_dir):
                continue
            for ver_entry in sorted(os.listdir(mp_dir)):
                ver_dir = os.path.join(mp_dir, ver_entry)
                if not os.path.isdir(ver_dir):
                    continue

                meta_path = os.path.join(ver_dir, "export_meta.json")
                meta = {}
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                    except Exception:
                        meta = {}

                files_info: List[Dict[str, str]] = []
                latest_mtime = float(meta.get("timestamp") or 0.0)
                for fname in sorted(os.listdir(ver_dir)):
                    if fname == "export_meta.json":
                        continue
                    fpath = os.path.join(ver_dir, fname)
                    if not os.path.isfile(fpath) or not fname.lower().endswith((".zip", ".json")):
                        continue
                    mtime = os.path.getmtime(fpath)
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                    rel_p = os.path.relpath(fpath, root_dir).replace("\\", "/")
                    size_kb = round(os.path.getsize(fpath) / 1024, 1)
                    if "Full_Bundle" in fname:
                        label = "📦 통합 번들 (.zip)"
                    elif "ResourcePack" in fname:
                        label = "🎨 리소스팩 (.zip)"
                    elif "Override" in fname:
                        label = "📂 Override (퀘스트+KubeJS .zip)"
                    elif "FTBQuests" in fname:
                        label = "📜 FTB Quests (.zip)"
                    elif "KubeJS" in fname:
                        label = "🧩 KubeJS 에셋 (.zip)"
                    elif "Mod_Archive" in fname:
                        label = "💾 번역 보관소 (.json)"
                    else:
                        label = fname
                    files_info.append({
                        "label": label,
                        "filename": fname,
                        "rel_path": rel_p,
                        "path": fpath,
                        "size_kb": size_kb,
                    })

                if not files_info:
                    continue

                exported_at = meta.get("exported_at") or time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime)
                )
                history.append({
                    "modpack_name": meta.get("modpack_name") or mp_entry,
                    "modpack_version": meta.get("modpack_version") or ver_entry,
                    "mc_version": meta.get("mc_version") or "",
                    "pack_name": meta.get("pack_name") or f"{mp_entry}_{ver_entry}",
                    "exported_at": exported_at,
                    "timestamp": latest_mtime,
                    "export_dir": ver_dir,
                    "mod_namespaces_count": meta.get("mod_namespaces_count", 0),
                    "ftbquest_files_written": meta.get("ftbquest_files_written", 0),
                    "kubejs_files_written": meta.get("kubejs_files_written", 0),
                    "total_exported_keys": meta.get("total_exported_keys", 0),
                    "download_files": files_info,
                })

        history.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return history


