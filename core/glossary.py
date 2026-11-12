"""Hierarchical Glossary Manager for Minecraft Modpack Translation."""

from __future__ import annotations

import csv
import io
import json
import os
from typing import Dict, List, Optional, Tuple


class GlossaryManager:
    """Manages Vanilla, Global Mod, Mod-Specific, and User Custom glossaries."""

    def __init__(self, glossary_dir: str):
        self.glossary_dir = os.path.abspath(glossary_dir)
        os.makedirs(self.glossary_dir, exist_ok=True)

        self.vanilla_path = os.path.join(self.glossary_dir, "vanilla.json")
        self.global_path = os.path.join(self.glossary_dir, "global_mods.json")
        self.mods_path = os.path.join(self.glossary_dir, "mods.json")
        self.custom_csv_path = os.path.join(self.glossary_dir, "user_custom.csv")

        self.vanilla: Dict[str, str] = {}
        self.global_mods: Dict[str, str] = {}
        self.mod_specific: Dict[str, Dict[str, str]] = {}
        self.user_custom: List[Dict[str, str]] = []  # [{"original": ..., "translated": ..., "mod_id": ..., "note": ...}]

        self.reload()

    def reload(self) -> None:
        self.vanilla = self._load_json(self.vanilla_path, {})
        self.global_mods = self._load_json(self.global_path, {})
        self.mod_specific = self._load_json(self.mods_path, {})
        self.user_custom = self._load_custom_csv()

    @staticmethod
    def _load_json(path: str, default):
        if not os.path.isfile(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _load_custom_csv(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        if not os.path.isfile(self.custom_csv_path):
            return rows
        try:
            with open(self.custom_csv_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    orig = (row.get("Original") or row.get("original") or "").strip()
                    trans = (row.get("Translated") or row.get("translated") or "").strip()
                    mod_id = (row.get("ModID") or row.get("mod_id") or "global").strip() or "global"
                    note = (row.get("Note") or row.get("note") or "").strip()
                    if orig and trans:
                        rows.append({
                            "original": orig,
                            "translated": trans,
                            "mod_id": mod_id,
                            "note": note,
                        })
        except Exception:
            pass
        return rows

    def save_custom_csv(self) -> None:
        with open(self.custom_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Original", "Translated", "ModID", "Note"])
            for item in self.user_custom:
                writer.writerow([
                    item.get("original", ""),
                    item.get("translated", ""),
                    item.get("mod_id", "global"),
                    item.get("note", ""),
                ])

    def save_mod_specific(self) -> None:
        with open(self.mods_path, "w", encoding="utf-8") as f:
            json.dump(self.mod_specific, f, ensure_ascii=False, indent=2)

    def add_or_update_custom_term(
        self, original: str, translated: str, mod_id: str = "global", note: str = ""
    ) -> None:
        original = original.strip()
        translated = translated.strip()
        mod_id = (mod_id or "global").strip().lower()
        if not original or not translated:
            return

        # Update existing if matched
        for item in self.user_custom:
            if item["original"].lower() == original.lower() and item["mod_id"].lower() == mod_id:
                item["translated"] = translated
                if note:
                    item["note"] = note
                self.save_custom_csv()
                return

        self.user_custom.append({
            "original": original,
            "translated": translated,
            "mod_id": mod_id,
            "note": note,
        })
        self.save_custom_csv()

    def delete_custom_term(self, original: str, mod_id: str = "global") -> bool:
        before = len(self.user_custom)
        self.user_custom = [
            item for item in self.user_custom
            if not (item["original"].lower() == original.strip().lower() and item["mod_id"].lower() == mod_id.strip().lower())
        ]
        if len(self.user_custom) != before:
            self.save_custom_csv()
            return True
        return False

    def import_csv_text(self, csv_text: str) -> int:
        """Import CSV lines (Original,Translated,ModID,Note) into user_custom."""
        count = 0
        reader = csv.reader(io.StringIO(csv_text.strip()))
        for idx, row in enumerate(reader):
            if not row or len(row) < 2:
                continue
            if idx == 0 and row[0].strip().lower() in ("original", "원문", "en", "english"):
                continue
            orig = row[0].strip()
            trans = row[1].strip()
            mod_id = row[2].strip() if len(row) > 2 and row[2].strip() else "global"
            note = row[3].strip() if len(row) > 3 else ""
            if orig and trans:
                self.add_or_update_custom_term(orig, trans, mod_id, note)
                count += 1
        return count

    def get_summary(self) -> dict:
        mod_counts = {m: len(terms) for m, terms in self.mod_specific.items()}
        return {
            "vanilla_count": len(self.vanilla),
            "global_mods_count": len(self.global_mods),
            "mod_specific_mods": len(self.mod_specific),
            "mod_specific_total_terms": sum(mod_counts.values()),
            "mod_counts": mod_counts,
            "user_custom_count": len(self.user_custom),
            "user_custom": self.user_custom,
            "mod_specific": self.mod_specific,
            "vanilla": self.vanilla,
            "global_mods": self.global_mods,
        }

    def get_relevant_terms_for_chunk(
        self, mod_id: str, chunk_values: Optional[List[str]] = None, max_terms: int = 80
    ) -> Dict[str, str]:
        """
        Build a merged glossary tailored to `mod_id` and `chunk_values`.
        Priority (highest wins):
        1. User Custom (for this mod_id)
        2. User Custom (global)
        3. Mod-Specific (for this mod_id or related family prefix)
        4. Global Mods
        5. Vanilla Minecraft
        If chunk_values is provided, filters general terms to those actually appearing in the chunk!
        """
        mod_ids_clean = [
            m.strip().lower().replace("mod:", "").replace("kubejs:", "")
            for m in (mod_id or "").split(",")
            if m.strip()
        ]
        combined_text_lower = " \n ".join(chunk_values).lower() if chunk_values else ""

        selected: Dict[str, str] = {}

        # Helper to check if term appears in chunk text
        def appears_in_chunk(term: str) -> bool:
            if not combined_text_lower:
                return True
            return term.lower() in combined_text_lower

        # 1. Include mod-specific terms for any mod in this chunk (or matching prefix like ae2 -> advanced_ae)
        for m_key, m_dict in self.mod_specific.items():
            if any(m_key == mc or m_key in mc or mc in m_key for mc in mod_ids_clean):
                for orig, trans in m_dict.items():
                    if not combined_text_lower or appears_in_chunk(orig) or (len(mod_ids_clean) == 1 and len(selected) < 25):
                        selected[orig] = trans

        # 2. Include global & vanilla terms that actually appear in this chunk
        for orig, trans in self.global_mods.items():
            if appears_in_chunk(orig):
                selected.setdefault(orig, trans)

        for orig, trans in self.vanilla.items():
            if appears_in_chunk(orig):
                selected.setdefault(orig, trans)

        # 3. Override with User Custom (global first, then mod-specific)
        for item in self.user_custom:
            if item["mod_id"].lower() in ("global", "*", "all"):
                if not combined_text_lower or appears_in_chunk(item["original"]):
                    selected[item["original"]] = item["translated"]

        for item in self.user_custom:
            if item["mod_id"].lower() in mod_ids_clean:
                if not combined_text_lower or appears_in_chunk(item["original"]) or len(mod_ids_clean) == 1:
                    selected[item["original"]] = item["translated"]

        # Cap at max_terms (keep longest/most specific phrases first)
        if len(selected) > max_terms:
            sorted_keys = sorted(selected.keys(), key=lambda k: len(k), reverse=True)[:max_terms]
            selected = {k: selected[k] for k in sorted_keys}

        return selected

    def format_glossary_for_prompt(self, mod_id: str, chunk_values: Optional[List[str]] = None) -> str:
        terms = self.get_relevant_terms_for_chunk(mod_id, chunk_values)
        if not terms:
            return ""
        lines = [f"- {orig} -> {trans}" for orig, trans in terms.items()]
        return "\n".join(lines)
