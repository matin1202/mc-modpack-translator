"""SQLite Translation Memory (TM), Version-Aware Mod Archive, and Project Cache Manager."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple


class TranslationCache:
    """
    Thread-safe SQLite database + persistent per-mod JSON archive (`data/mod_archive/`).
    Stores `(entry_id, lang_key, en_text, ko_text)` so that:
    1. When switching to another modpack or updating a mod's version, unchanged keys are 100% reused.
    2. If a mod update changes the English text (`en_text`) of an existing key, it is detected as modified
       and queued for re-translation instead of serving stale translations.
    3. If a mod renames a key but keeps the same `en_text`, it is automatically matched from the archive.
    """

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.data_dir = os.path.dirname(self.db_path)
        self.mod_archive_dir = os.path.join(self.data_dir, "mod_archive")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.mod_archive_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._sync_archive_files_on_startup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    entry_id TEXT NOT NULL,
                    lang_key TEXT NOT NULL,
                    en_text TEXT NOT NULL,
                    ko_text TEXT NOT NULL,
                    provider TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (entry_id, lang_key)
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_translations_entry ON translations(entry_id);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_memory (
                    en_text TEXT PRIMARY KEY,
                    ko_text TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.commit()

    @staticmethod
    def _archive_filename_for_entry(entry_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "__", entry_id.strip())
        return f"{safe}.json"

    def _write_mod_archive_file(self, entry_id: str) -> None:
        """Persist a single entry's translations (with original en_text) to data/mod_archive/<entry_id>.json."""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT lang_key, en_text, ko_text, provider, updated_at FROM translations WHERE entry_id = ? ORDER BY lang_key",
                    (entry_id,),
                ).fetchall()
            if not rows:
                return
            archive_path = os.path.join(self.mod_archive_dir, self._archive_filename_for_entry(entry_id))
            payload = {
                "entry_id": entry_id,
                "updated_at": time.time(),
                "total_keys": len(rows),
                "translations": {
                    r[0]: {"en": r[1], "ko": r[2], "provider": r[3]}
                    for r in rows
                },
            }
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _sync_archive_files_on_startup(self) -> None:
        """
        On startup:
        1. Import any `.json` files placed into `data/mod_archive/` that aren't in SQLite yet.
        2. Ensure all SQLite entries have up-to-date `.json` files in `data/mod_archive/`.
        """
        try:
            # 1. Load from disk archive files if any new ones exist
            for fname in os.listdir(self.mod_archive_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(self.mod_archive_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    entry_id = data.get("entry_id")
                    trans_map = data.get("translations", {})
                    if not entry_id or not isinstance(trans_map, dict):
                        continue
                    items_to_insert = []
                    for k, obj in trans_map.items():
                        if isinstance(obj, dict) and obj.get("en") and obj.get("ko"):
                            items_to_insert.append((k, str(obj["en"]), str(obj["ko"]), str(obj.get("provider", "archive"))))
                    if items_to_insert:
                        now = time.time()
                        with self._lock, self._connect() as conn:
                            conn.executemany(
                                """
                                INSERT OR IGNORE INTO translations (entry_id, lang_key, en_text, ko_text, provider, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?);
                                """,
                                [(entry_id, k, en, ko, prov, now) for k, en, ko, prov in items_to_insert],
                            )
                            conn.executemany(
                                """
                                INSERT OR IGNORE INTO translation_memory (en_text, ko_text, source, updated_at)
                                VALUES (?, ?, ?, ?);
                                """,
                                [(en, ko, f"{entry_id}:{prov}", now) for _, en, ko, prov in items_to_insert],
                            )
                            conn.commit()
                except Exception:
                    continue

            # 2. Ensure every entry in SQLite is dumped to data/mod_archive/
            with self._lock, self._connect() as conn:
                entry_ids = [r[0] for r in conn.execute("SELECT DISTINCT entry_id FROM translations").fetchall()]
            for eid in entry_ids:
                archive_path = os.path.join(self.mod_archive_dir, self._archive_filename_for_entry(eid))
                if not os.path.isfile(archive_path):
                    self._write_mod_archive_file(eid)
        except Exception:
            pass

    def get_all_entry_translations(self) -> Dict[str, Dict[str, str]]:
        """Return {entry_id: {lang_key: ko_text}} for all cached translations."""
        result: Dict[str, Dict[str, str]] = {}
        with self._lock, self._connect() as conn:
            cur = conn.execute("SELECT entry_id, lang_key, ko_text FROM translations")
            for entry_id, lang_key, ko_text in cur.fetchall():
                result.setdefault(entry_id, {})[lang_key] = ko_text
        return result

    def get_all_entry_translations_with_en(self) -> Dict[str, Dict[str, Tuple[str, str]]]:
        """Return {entry_id: {lang_key: (en_text, ko_text)}} for version-aware comparison."""
        result: Dict[str, Dict[str, Tuple[str, str]]] = {}
        with self._lock, self._connect() as conn:
            cur = conn.execute("SELECT entry_id, lang_key, en_text, ko_text FROM translations")
            for entry_id, lang_key, en_text, ko_text in cur.fetchall():
                result.setdefault(entry_id, {})[lang_key] = (en_text, ko_text)
        return result

    def get_entry_translations(self, entry_id: str) -> Dict[str, str]:
        """Return {lang_key: ko_text} for a single entry_id."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT lang_key, ko_text FROM translations WHERE entry_id = ?",
                (entry_id,),
            )
            return {k: v for k, v in cur.fetchall()}

    def get_entry_translations_with_en(self, entry_id: str) -> Dict[str, Tuple[str, str]]:
        """Return {lang_key: (en_text, ko_text)} for a single entry_id."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT lang_key, en_text, ko_text FROM translations WHERE entry_id = ?",
                (entry_id,),
            )
            return {k: (en, ko) for k, en, ko in cur.fetchall()}

    def get_entry_details(self, entry_id: str) -> List[Dict[str, str]]:
        """Return detailed rows for a single entry_id."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT lang_key, en_text, ko_text, provider, updated_at FROM translations WHERE entry_id = ? ORDER BY lang_key",
                (entry_id,),
            )
            return [
                {
                    "lang_key": r[0],
                    "en_text": r[1],
                    "ko_text": r[2],
                    "provider": r[3],
                    "updated_at": r[4],
                }
                for r in cur.fetchall()
            ]

    def save_translations_batch(
        self,
        entry_id: str,
        items: List[Tuple[str, str, str]],  # [(lang_key, en_text, ko_text), ...]
        provider: str = "llm",
    ) -> int:
        """Save a batch of translations for an entry_id, update global TM, and sync mod_archive JSON."""
        if not items:
            return 0
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO translations (entry_id, lang_key, en_text, ko_text, provider, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id, lang_key) DO UPDATE SET
                    en_text=excluded.en_text,
                    ko_text=excluded.ko_text,
                    provider=excluded.provider,
                    updated_at=excluded.updated_at;
                """,
                [(entry_id, k, en, ko, provider, now) for k, en, ko in items],
            )
            conn.executemany(
                """
                INSERT INTO translation_memory (en_text, ko_text, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(en_text) DO UPDATE SET
                    ko_text=excluded.ko_text,
                    source=excluded.source,
                    updated_at=excluded.updated_at;
                """,
                [(en, ko, f"{entry_id}:{provider}", now) for _, en, ko in items if en and ko],
            )
            conn.commit()

        # Persist per-mod archive file so it can be reused across modpacks/versions
        self._write_mod_archive_file(entry_id)
        return len(items)

    def seed_translation_memory(self, en_ko_pairs: Dict[str, str], source: str = "builtin_jar") -> int:
        """Seed global translation_memory from built-in ko_kr.json files without overwriting existing TM."""
        if not en_ko_pairs:
            return 0
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO translation_memory (en_text, ko_text, source, updated_at)
                VALUES (?, ?, ?, ?);
                """,
                [(en, ko, source, now) for en, ko in en_ko_pairs.items() if en and ko],
            )
            conn.commit()
        return len(en_ko_pairs)

    def lookup_tm_batch(self, en_texts: List[str]) -> Dict[str, str]:
        """Look up exact matches in translation_memory."""
        if not en_texts:
            return {}
        found: Dict[str, str] = {}
        unique_list = list(set(en_texts))
        with self._lock, self._connect() as conn:
            for i in range(0, len(unique_list), 500):
                batch = unique_list[i : i + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    f"SELECT en_text, ko_text FROM translation_memory WHERE en_text IN ({placeholders})",
                    batch,
                )
                for en_val, ko_val in cur.fetchall():
                    found[en_val] = ko_val
        return found

    def export_portable_archive_dict(self, only_entry_ids: Optional[List[str]] = None) -> dict:
        """Export saved per-mod translations into a portable JSON dictionary (optionally filtered by entry_ids)."""
        allowed = set(only_entry_ids) if only_entry_ids is not None else None
        entries_data: Dict[str, Dict[str, dict]] = {}
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT entry_id, lang_key, en_text, ko_text, provider FROM translations ORDER BY entry_id, lang_key"
            )
            for entry_id, lang_key, en_text, ko_text, provider in cur.fetchall():
                if allowed is not None and entry_id not in allowed:
                    continue
                entries_data.setdefault(entry_id, {})[lang_key] = {
                    "en": en_text,
                    "ko": ko_text,
                    "provider": provider,
                }
        return {
            "format": "mc_translator_mod_archive_v1",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_entries": len(entries_data),
            "total_keys": sum(len(v) for v in entries_data.values()),
            "entries": entries_data,
        }

    def import_portable_archive_dict(self, data: dict, overwrite: bool = True) -> dict:
        """Import a portable mod archive JSON dictionary into SQLite and data/mod_archive/."""
        entries_data = data.get("entries", {})
        if not isinstance(entries_data, dict):
            raise ValueError("올바른 모드 번역 아카이브 JSON 형식이 아닙니다.")

        imported_entries = 0
        imported_keys = 0
        now = time.time()

        with self._lock, self._connect() as conn:
            for entry_id, keys_map in entries_data.items():
                if not isinstance(keys_map, dict):
                    continue
                rows = []
                tm_rows = []
                for k, obj in keys_map.items():
                    if isinstance(obj, dict) and obj.get("en") and obj.get("ko"):
                        en_v = str(obj["en"])
                        ko_v = str(obj["ko"])
                        prov = str(obj.get("provider", "imported_archive"))
                        rows.append((entry_id, k, en_v, ko_v, prov, now))
                        tm_rows.append((en_v, ko_v, f"{entry_id}:{prov}", now))
                if rows:
                    imported_entries += 1
                    imported_keys += len(rows)
                    if overwrite:
                        conn.executemany(
                            """
                            INSERT INTO translations (entry_id, lang_key, en_text, ko_text, provider, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(entry_id, lang_key) DO UPDATE SET
                                en_text=excluded.en_text,
                                ko_text=excluded.ko_text,
                                provider=excluded.provider,
                                updated_at=excluded.updated_at;
                            """,
                            rows,
                        )
                    else:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO translations (entry_id, lang_key, en_text, ko_text, provider, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            rows,
                        )
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO translation_memory (en_text, ko_text, source, updated_at)
                        VALUES (?, ?, ?, ?);
                        """,
                        tm_rows,
                    )
            conn.commit()

        for entry_id in entries_data.keys():
            self._write_mod_archive_file(entry_id)

        return {
            "imported_entries": imported_entries,
            "imported_keys": imported_keys,
        }

    def get_stats(self) -> dict:
        with self._lock, self._connect() as conn:
            t_count = conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
            e_count = conn.execute("SELECT COUNT(DISTINCT entry_id) FROM translations").fetchone()[0]
            tm_count = conn.execute("SELECT COUNT(*) FROM translation_memory").fetchone()[0]
        archive_files = 0
        if os.path.isdir(self.mod_archive_dir):
            archive_files = len([f for f in os.listdir(self.mod_archive_dir) if f.endswith(".json")])
        return {
            "translated_keys_count": t_count,
            "translated_entries_count": e_count,
            "tm_unique_pairs_count": tm_count,
            "archive_files_count": archive_files,
            "mod_archive_dir": self.mod_archive_dir,
        }

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def get_all_settings(self) -> Dict[str, str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            return {k: v for k, v in rows}

