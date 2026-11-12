"""Modpack Scanner: Extracts language files and metadata from Mod JARs, FTB Quests, and KubeJS."""

from __future__ import annotations

import glob
import json
import os
import re
import tomllib
import zipfile
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


def _decode_snbt_string_token(tok: str) -> str:
    """Decode a double-quoted SNBT string token into a Python str."""
    try:
        return json.loads(tok)
    except Exception:
        inner = tok[1:-1] if len(tok) >= 2 else tok
        return (
            inner.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )


def parse_snbt_lang(raw_text: str) -> dict:
    """
    Parse FTB Quests SNBT language files (e.g., `config/ftbquests/quests/lang/en_us.snbt`),
    supporting unquoted keys (`quest.XXXX.title:`), comma-less lines, and string arrays (`[ "..." "..." ]`).
    """
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]

    # Tokenize double-quoted strings, comments, structural punctuation, or unquoted keys
    tok_re = re.compile(
        r'("(?:\\.|[^"\\])*")'           # Group 1: Double-quoted string
        r"|(\/\/[^\r\n]*|#[^\r\n]*|\/\*[\s\S]*?\*\/)"  # Group 2: Comment
        r"|([\{\}\[\]:,])"               # Group 3: Structural char
        r"|([a-zA-Z0-9_.\-+]+)",         # Group 4: Unquoted key/literal
        re.MULTILINE,
    )

    tokens: List[Tuple[str, str]] = []
    for m in tok_re.finditer(raw_text):
        if m.group(2) is not None:
            continue  # Skip comments
        if m.group(1) is not None:
            tokens.append(("STR", m.group(1)))
        elif m.group(3) is not None:
            tokens.append(("SYM", m.group(3)))
        elif m.group(4) is not None:
            tokens.append(("IDENT", m.group(4)))

    result: dict = {}
    n = len(tokens)
    i = 0

    # Find opening '{'
    while i < n and not (tokens[i][0] == "SYM" and tokens[i][1] == "{"):
        i += 1
    i += 1  # Skip '{'

    while i < n:
        ttype, tval = tokens[i]
        if ttype == "SYM" and tval == "}":
            break
        if ttype == "SYM" and tval == ",":
            i += 1
            continue

        # Expect a key (IDENT or STR)
        if ttype not in ("IDENT", "STR"):
            i += 1
            continue
        key = _decode_snbt_string_token(tval) if ttype == "STR" else tval
        i += 1

        # Expect ':'
        if i < n and tokens[i] == ("SYM", ":"):
            i += 1
        else:
            continue

        if i >= n:
            break

        vtype, vval = tokens[i]
        if vtype == "STR":
            result[key] = _decode_snbt_string_token(vval)
            i += 1
        elif vtype == "SYM" and vval == "[":
            i += 1
            arr: List[str] = []
            while i < n:
                at, av = tokens[i]
                if at == "SYM" and av == "]":
                    i += 1
                    break
                if at == "SYM" and av == ",":
                    i += 1
                    continue
                if at == "STR":
                    arr.append(_decode_snbt_string_token(av))
                i += 1
            result[key] = arr
        else:
            i += 1

    return result


def serialize_snbt_lang(data: dict) -> str:
    """Serialize a dictionary of `{key: str | List[str]}` into FTB Quests `.snbt` language format."""
    if not data:
        return "{ }\n"
    key_ident_re = re.compile(r"^[a-zA-Z0-9_.\-]+$")
    lines = ["{"]
    for k, v in data.items():
        k_str = str(k)
        k_out = k_str if key_ident_re.match(k_str) else json.dumps(k_str, ensure_ascii=False)
        if isinstance(v, list):
            if not v:
                lines.append(f"\t{k_out}: [ ]")
            elif len(v) == 1:
                lines.append(f"\t{k_out}: [{json.dumps(str(v[0]), ensure_ascii=False)}]")
            else:
                lines.append(f"\t{k_out}: [")
                for item in v:
                    lines.append(f"\t\t{json.dumps(str(item), ensure_ascii=False)}")
                lines.append("\t]")
        elif isinstance(v, str):
            lines.append(f"\t{k_out}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"\t{k_out}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("}\n")
    return "\n".join(lines)


def parse_relaxed_json(raw_text: str) -> dict:
    """Parse JSON, JSON5, or FTB Quests SNBT language files."""
    # Strip UTF-8 BOM if present
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]

    # Fast path: standard JSON
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Remove single-line // comments and multi-line /* */ comments outside of strings
    pattern = re.compile(
        r'("(?:\\.|[^"\\])*")|(\/\/[^\r\n]*|\/\*[\s\S]*?\*\/)',
        re.MULTILINE,
    )
    cleaned = pattern.sub(lambda m: m.group(1) if m.group(1) is not None else "", raw_text)

    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Fallback: FTB Quests SNBT format (unquoted keys, no commas)
    try:
        snbt_data = parse_snbt_lang(raw_text)
        if isinstance(snbt_data, dict) and snbt_data:
            return snbt_data
    except Exception:
        pass
    return {}


def flatten_lang_dict(data: dict) -> Dict[str, str]:
    """Flatten string and list-of-string values (used in FTB Quests json5) into flat key -> string pairs."""
    flat: Dict[str, str] = {}
    for k, v in data.items():
        if k.startswith("_comment") or k.startswith("//"):
            continue
        if isinstance(v, str):
            if v.strip():
                flat[str(k)] = v
        elif isinstance(v, list):
            for idx, item in enumerate(v):
                if isinstance(item, str) and item.strip():
                    flat[f"{k}__list_idx_{idx}"] = item
    return flat


def unflatten_to_original_shape(original_data: dict, translated_flat: Dict[str, str], fallback_en: bool = True) -> dict:
    """Reconstruct original dict/list structure (e.g., for FTB Quests json5) using translated flat keys."""
    result = {}
    for k, v in original_data.items():
        if isinstance(v, str):
            if k in translated_flat and translated_flat[k].strip():
                result[k] = translated_flat[k]
            elif fallback_en:
                result[k] = v
        elif isinstance(v, list):
            new_list = []
            for idx, item in enumerate(v):
                flat_key = f"{k}__list_idx_{idx}"
                if isinstance(item, str) and flat_key in translated_flat and translated_flat[flat_key].strip():
                    new_list.append(translated_flat[flat_key])
                else:
                    new_list.append(item)
            result[k] = new_list
        else:
            result[k] = v
    return result


def estimate_tokens(text: str) -> int:
    """Estimate token count for English/Korean/JSON strings (~3.6 chars per token on average)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.6) + 1)


_PATCHOULI_TRANSLATABLE_KEYS = {
    "name",
    "description",
    "title",
    "subtitle",
    "text",
    "header",
    "landing_text",
}
_RAW_ID_OR_LANG_KEY_RE = re.compile(r"^[a-z0-9_.:/\-]+$")
_ALPHA_CHECK_RE = re.compile(r"[a-zA-Z]{2,}")
_HANGUL_CHECK_RE = re.compile(r"[가-힣]")
_BOOK_LINK_RE = re.compile(r"\((?:entry|category)://[^)\s]+\)")


def _filter_valid_builtin_ko(en_flat: Dict[str, str], raw_ko_flat: Dict[str, str]) -> Dict[str, str]:
    """
    Filter built-in `ko_kr` entries so that:
    1. Obsolete keys that no longer exist in `en_flat` are excluded.
    2. Crowdin / placeholder entries that contain no Hangul (`[가-힣]`) while `en_flat` has English letters
       are excluded so they are properly queued for Korean translation.
    3. Entries whose Modonomicon/book markdown links (`(entry://...)`, `(category://...)`) do not match
       the current `en_flat` links (e.g. outdated built-in `ko_kr.json` from an older mod version) are excluded.
    """
    if not raw_ko_flat or not en_flat:
        return {}
    valid_ko: Dict[str, str] = {}
    for k, ko_val in raw_ko_flat.items():
        if k not in en_flat or not isinstance(ko_val, str) or not ko_val.strip():
            continue
        en_val = en_flat[k]
        if _ALPHA_CHECK_RE.search(en_val) and not _HANGUL_CHECK_RE.search(ko_val):
            continue
        if (
            "entry://" in en_val
            or "category://" in en_val
            or "entry://" in ko_val
            or "category://" in ko_val
        ):
            if set(_BOOK_LINK_RE.findall(en_val)) != set(_BOOK_LINK_RE.findall(ko_val)):
                continue
        valid_ko[k] = ko_val
    return valid_ko


def _is_translatable_patchouli_string(val: str) -> bool:
    """
    Return True if `val` is human-readable English text rather than a raw resource location
    (`minecraft:stone`) or unspaced lang key (`booklet.actuallyadditions.chapter.intro.text.1`).
    """
    if not val or not isinstance(val, str):
        return False
    s = val.strip()
    if not s or not _ALPHA_CHECK_RE.search(s):
        return False
    # If there are no spaces and it looks like a resource location or dotted lang key, skip it!
    if " " not in s and _RAW_ID_OR_LANG_KEY_RE.match(s) and ("." in s or ":" in s or "/" in s or "_" in s):
        return False
    return True


_SNBT_QUEST_SCAN_RE = re.compile(
    r'(\/\/[^\r\n]*|#[^\r\n]*|\/\*[\s\S]*?\*\/)'  # Group 1: Comment (skip)
    r'|\b(title|subtitle|description|hover)(\s*:\s*)'
    r'(?:'
    r'("(?:\\.|[^"\\])*")'                        # Group 4: Single string value
    r'|'
    r'(\[(?:(?:"(?:\\.|[^"\\])*")|[^"\]])*\])'    # Group 5: Array of strings value
    r')'
    r'|("(?:\\.|[^"\\])*")',                      # Group 6: Any other quoted string (skip)
    re.MULTILINE,
)
_SNBT_STR_TOKEN_RE = re.compile(r'("(?:\\.|[^"\\])*")')
_SNBT_BRACE_KEY_RE = re.compile(r"^\{([a-zA-Z0-9_.\-:]+)[\}\)]$")
_MC_COLOR_CODE_RE = re.compile(r"(?:\\\\)?[&§][0-9a-fk-orA-FK-OR]")
_FTB_TAG_STRIP_RE = re.compile(r"\{(?:@[a-zA-Z0-9_]+|image:[^}]+)\}")
_GT_TIER_ACRONYMS = {
    "ULV", "LV", "MV", "HV", "EV", "IV", "LUV", "ZPM",
    "UV", "UHV", "UEV", "UIV", "UXV", "OPV", "MAX",
    "FE", "EU", "RF",
}


def _is_translatable_snbt_inline(val: str) -> bool:
    """Return True if an inline SNBT quest string is human-readable English prose/title."""
    if not val or not isinstance(val, str):
        return False
    s = _FTB_TAG_STRIP_RE.sub("", _MC_COLOR_CODE_RE.sub("", val)).strip()
    if s.upper() in _GT_TIER_ACRONYMS:
        return False
    return _is_translatable_patchouli_string(s)


def extract_snbt_quest_keys(
    raw_snbt_text: str,
    ref_en_lookup: Optional[Dict[str, str]] = None,
    file_prefix: str = "",
) -> Dict[str, str]:
    """
    Extract translatable strings from a direct FTB Quests `.snbt` quest file (`chapters/*.snbt`,
    `chapter_groups.snbt`, `data.snbt`, `reward_tables/*.snbt`), supporting:
    1. Brace-wrapped lang keys (`"{moni.quest.xxx.title}"`, `"{atm9.quest.xxx}"`) resolved via `ref_en_lookup`
    2. JSON text components (`"[\"\",{\"translate\":\"moni.quest.xxx.rich_description1\"},...]"`)
    3. Inline English strings (`title: "&9Monifactory"`, `title: "Any Exoskeleton"`, etc.)
    """
    flat: Dict[str, str] = {}
    if not raw_snbt_text:
        return flat
    ref_map = ref_en_lookup or {}
    prop_idx = 0

    def _extract_from_token(tok: str, p_idx: int, e_idx: int) -> None:
        s = _decode_snbt_string_token(tok)
        s_strip = s.strip()
        if not s_strip or s_strip == "{@pagebreak}" or s_strip.startswith("{image:"):
            return

        bm = _SNBT_BRACE_KEY_RE.match(s_strip)
        if bm:
            ref_key = bm.group(1)
            if ref_key in ref_map and ref_map[ref_key].strip():
                flat[ref_key] = ref_map[ref_key]
            return

        if (s_strip.startswith("[") and s_strip.endswith("]")) or (
            s_strip.startswith("{") and s_strip.endswith("}")
        ):
            try:
                comp = json.loads(s_strip)
                if isinstance(comp, (list, dict)):
                    sub_idx = 0

                    def _walk_comp(node: object) -> None:
                        nonlocal sub_idx
                        if isinstance(node, dict):
                            tr_k = node.get("translate")
                            if isinstance(tr_k, str) and tr_k.strip():
                                if tr_k in ref_map and ref_map[tr_k].strip():
                                    flat[tr_k] = ref_map[tr_k]
                                elif _is_translatable_snbt_inline(tr_k):
                                    flat[f"__inline__{file_prefix}{p_idx}_{e_idx}_{sub_idx}"] = tr_k
                                sub_idx += 1
                            elif isinstance(node.get("text"), str) and node["text"].strip():
                                txt_v = node["text"]
                                if _is_translatable_snbt_inline(txt_v):
                                    flat[f"__inline__{file_prefix}{p_idx}_{e_idx}_{sub_idx}"] = txt_v
                                sub_idx += 1
                            for v in node.values():
                                if isinstance(v, (dict, list)):
                                    _walk_comp(v)
                        elif isinstance(node, list):
                            for item in node:
                                if isinstance(item, (dict, list)):
                                    _walk_comp(item)

                    _walk_comp(comp)
                    return
            except Exception:
                pass

        if _is_translatable_snbt_inline(s):
            flat[f"__inline__{file_prefix}{p_idx}_{e_idx}"] = s

    for m in _SNBT_QUEST_SCAN_RE.finditer(raw_snbt_text):
        if m.group(2) is None:
            continue
        single_tok = m.group(4)
        arr_tok = m.group(5)
        if single_tok is not None:
            _extract_from_token(single_tok, prop_idx, 0)
        elif arr_tok is not None:
            for elem_idx, sm in enumerate(_SNBT_STR_TOKEN_RE.finditer(arr_tok)):
                _extract_from_token(sm.group(1), prop_idx, elem_idx)
        prop_idx += 1

    return flat


def reconstruct_snbt_quest_text(
    raw_snbt_text: str,
    translated_flat: Dict[str, str],
    en_to_ko_fallback: Optional[Dict[str, str]] = None,
    file_prefix: str = "",
) -> str:
    """
    Reconstruct a direct FTB Quests `.snbt` quest file by replacing `{key}` references, JSON text
    components (`"translate"` / `"text"`), and inline strings with their Korean translations while
    preserving all SNBT numeric suffixes (`1.5d`, `1600000L`, `1b`), IDs, and formatting intact.
    """
    if not raw_snbt_text or not translated_flat:
        return raw_snbt_text
    en_fallback = en_to_ko_fallback or {}
    prop_idx = 0

    def _safe_snbt_str_value(val: str) -> str:
        v = val.strip()
        # Prevent FTB Quests from mistaking a literal Korean "{...}" string for an I18n key
        if v.startswith("{") and v.endswith("}") and not v.startswith("{@") and not v.startswith("{image:"):
            return f"&r{val}"
        return val

    def _replace_token(tok: str, p_idx: int, e_idx: int) -> str:
        s = _decode_snbt_string_token(tok)
        s_strip = s.strip()
        if not s_strip or s_strip == "{@pagebreak}" or s_strip.startswith("{image:"):
            return tok

        bm = _SNBT_BRACE_KEY_RE.match(s_strip)
        if bm:
            ref_key = bm.group(1)
            ko_v = translated_flat.get(ref_key, "")
            if ko_v and ko_v.strip():
                return json.dumps(_safe_snbt_str_value(ko_v), ensure_ascii=False)
            return tok

        if (s_strip.startswith("[") and s_strip.endswith("]")) or (
            s_strip.startswith("{") and s_strip.endswith("}")
        ):
            try:
                comp = json.loads(s_strip)
                if isinstance(comp, (list, dict)):
                    sub_idx = 0
                    modified = False

                    def _walk_replace_comp(node: object) -> None:
                        nonlocal sub_idx, modified
                        if isinstance(node, dict):
                            tr_k = node.get("translate")
                            if isinstance(tr_k, str) and tr_k.strip():
                                inline_k = f"__inline__{file_prefix}{p_idx}_{e_idx}_{sub_idx}"
                                ko_v = (
                                    translated_flat.get(tr_k)
                                    or translated_flat.get(inline_k)
                                    or en_fallback.get(tr_k)
                                )
                                if ko_v and ko_v.strip() and "with" not in node:
                                    node.pop("translate", None)
                                    # Vanilla JSON text components use '§' rather than '&' for color codes
                                    node["text"] = re.sub(r"&([0-9a-fk-orA-FK-OR])", r"§\1", ko_v)
                                    modified = True
                                sub_idx += 1
                            elif isinstance(node.get("text"), str) and node["text"].strip():
                                txt_v = node["text"]
                                inline_k = f"__inline__{file_prefix}{p_idx}_{e_idx}_{sub_idx}"
                                ko_v = translated_flat.get(inline_k) or en_fallback.get(txt_v)
                                if ko_v and ko_v.strip():
                                    node["text"] = ko_v
                                    modified = True
                                sub_idx += 1
                            for v in node.values():
                                if isinstance(v, (dict, list)):
                                    _walk_replace_comp(v)
                        elif isinstance(node, list):
                            for item in node:
                                if isinstance(item, (dict, list)):
                                    _walk_replace_comp(item)

                    _walk_replace_comp(comp)
                    if modified:
                        new_json_str = json.dumps(comp, ensure_ascii=False, separators=(",", ":"))
                        return json.dumps(new_json_str, ensure_ascii=False)
                    return tok
            except Exception:
                pass

        inline_k = f"__inline__{file_prefix}{p_idx}_{e_idx}"
        ko_v = translated_flat.get(inline_k) or en_fallback.get(s)
        if ko_v and ko_v.strip():
            return json.dumps(_safe_snbt_str_value(ko_v), ensure_ascii=False)
        return tok

    def _prop_replacer(m: re.Match) -> str:
        nonlocal prop_idx
        if m.group(2) is None:
            return m.group(0)
        p_idx = prop_idx
        prop_idx += 1
        prop_name = m.group(2)
        colon_sep = m.group(3)
        single_tok = m.group(4)
        arr_tok = m.group(5)
        if single_tok is not None:
            new_tok = _replace_token(single_tok, p_idx, 0)
            return f"{prop_name}{colon_sep}{new_tok}"
        if arr_tok is not None:
            elem_idx = 0

            def _arr_elem_replacer(sm: re.Match) -> str:
                nonlocal elem_idx
                e_idx = elem_idx
                elem_idx += 1
                return _replace_token(sm.group(1), p_idx, e_idx)

            new_arr = _SNBT_STR_TOKEN_RE.sub(_arr_elem_replacer, arr_tok)
            return f"{prop_name}{colon_sep}{new_arr}"
        return m.group(0)

    return _SNBT_QUEST_SCAN_RE.sub(_prop_replacer, raw_snbt_text)


def flatten_patchouli_json(data: dict, file_prefix: str = "") -> Dict[str, str]:
    """
    Extract translatable strings from a parsed Patchouli book JSON object (`categories/*.json`,
    `entries/**/*.json`, `templates/*.json`) using deterministic path keys.
    """
    flat: Dict[str, str] = {}
    prefix = f"{file_prefix}::" if file_prefix else ""

    def _walk(node, path_str: str, parent_key: str = ""):
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path_str}.{k}" if path_str else str(k)
                if isinstance(v, str):
                    if k in _PATCHOULI_TRANSLATABLE_KEYS and _is_translatable_patchouli_string(v):
                        flat[f"{prefix}{child_path}"] = v
                elif isinstance(v, (dict, list)):
                    _walk(v, child_path, k)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                child_path = f"{path_str}[{idx}]"
                if isinstance(item, str):
                    if parent_key in _PATCHOULI_TRANSLATABLE_KEYS and _is_translatable_patchouli_string(item):
                        flat[f"{prefix}{child_path}"] = item
                elif isinstance(item, (dict, list)):
                    _walk(item, child_path, parent_key)

    if isinstance(data, dict):
        _walk(data, "", "")
    return flat


def reconstruct_patchouli_json(original_data: dict, translated_flat: Dict[str, str], file_prefix: str = "") -> dict:
    """
    Reconstruct a Patchouli JSON object by replacing translated string fields in-place while
    preserving all structural fields (`icon`, `type`, `recipe`, `advancement`, `sortnum`, etc.).
    """
    prefix = f"{file_prefix}::" if file_prefix else ""

    def _walk_replace(node, path_str: str, parent_key: str = ""):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                child_path = f"{path_str}.{k}" if path_str else str(k)
                full_k = f"{prefix}{child_path}"
                if isinstance(v, str):
                    if full_k in translated_flat and translated_flat[full_k].strip():
                        out[k] = translated_flat[full_k]
                    else:
                        out[k] = v
                elif isinstance(v, (dict, list)):
                    out[k] = _walk_replace(v, child_path, k)
                else:
                    out[k] = v
            return out
        elif isinstance(node, list):
            out_list = []
            for idx, item in enumerate(node):
                child_path = f"{path_str}[{idx}]"
                full_k = f"{prefix}{child_path}"
                if isinstance(item, str):
                    if full_k in translated_flat and translated_flat[full_k].strip():
                        out_list.append(translated_flat[full_k])
                    else:
                        out_list.append(item)
                elif isinstance(item, (dict, list)):
                    out_list.append(_walk_replace(item, child_path, parent_key))
                else:
                    out_list.append(item)
            return out_list
        return node

    return _walk_replace(original_data, "", "") if isinstance(original_data, dict) else original_data


_FRONTMATTER_RE = re.compile(r"\A(---[ \t]*\r?\n)([\s\S]*?)(\r?\n---[ \t]*(?:\r?\n|$))")
_FM_NAV_TITLE_RE = re.compile(r"^([ \t]*title\s*:\s*)(.+?)([ \t]*\r?)$", re.MULTILINE)
_PURE_JSX_LINE_RE = re.compile(
    r"^[ \t]*(?:</?[A-Z][A-Za-z0-9_.]*(?:\s+[^>]*)?>|!\[[^\]]*\]\([^)]+\))[ \t]*$"
)


def extract_guideme_markdown_keys(md_text: str, file_prefix: str = "") -> Dict[str, str]:
    """
    Extract translatable keys (`nav_title` and prose/heading blocks `b_0`, `b_1`, ...) from a
    GuideMe `.md` guidebook file while leaving YAML frontmatter IDs, code fences, and standalone
    JSX component blocks (`<GameScene>`, `<Row>`, `<SubPages />`, `<RecipeFor ... />`) untouched.
    """
    flat: Dict[str, str] = {}
    if not md_text:
        return flat
    prefix = f"{file_prefix}::" if file_prefix else ""

    body_start = 0
    fm_match = _FRONTMATTER_RE.match(md_text)
    if fm_match:
        fm_content = fm_match.group(2)
        body_start = fm_match.end()
        t_match = _FM_NAV_TITLE_RE.search(fm_content)
        if t_match:
            raw_title = t_match.group(2).strip()
            if (raw_title.startswith('"') and raw_title.endswith('"')) or (
                raw_title.startswith("'") and raw_title.endswith("'")
            ):
                raw_title = raw_title[1:-1]
            if raw_title and _ALPHA_CHECK_RE.search(raw_title):
                flat[f"{prefix}nav_title"] = raw_title

    body = md_text[body_start:]
    lines = body.splitlines(keepends=True)

    in_code_fence = False
    in_game_scene = False
    current_para: List[str] = []
    block_idx = 0

    def _flush_para():
        nonlocal current_para, block_idx
        if not current_para:
            return
        joined = "".join(current_para).strip()
        current_para = []
        # Strip inline JSX tags before checking if there are actual English letters to translate
        stripped_jsx = re.sub(r"</?[A-Za-z][A-Za-z0-9_.]*(?:\s+[^>]*)?>", "", joined)
        if _ALPHA_CHECK_RE.search(stripped_jsx):
            flat[f"{prefix}b_{block_idx}"] = joined
        block_idx += 1

    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            _flush_para()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue

        if "<GameScene" in s:
            _flush_para()
            if "</GameScene>" not in s and not s.endswith("/>"):
                in_game_scene = True
            continue
        if in_game_scene:
            if "</GameScene>" in s:
                in_game_scene = False
            continue

        if not s:
            _flush_para()
            continue

        # Standalone JSX tag line (`<Row>`, `</Row>`, `<SubPages />`, `<BlockImage ... />`) or image `![...](...)`
        if _PURE_JSX_LINE_RE.match(s):
            _flush_para()
            continue

        # Headings (`# Title`) are flushed as their own standalone block
        if s.startswith("#"):
            _flush_para()
            current_para.append(line)
            _flush_para()
            continue

        # List items or table rows are flushed line-by-line so structure stays exact
        if s.startswith(("- ", "* ", "|")) or re.match(r"^\d+\.\s", s):
            _flush_para()
            current_para.append(line)
            _flush_para()
            continue

        current_para.append(line)

    _flush_para()
    return flat


_INLINE_CODE_SPLIT_RE = re.compile(r"(`+[^`]+`+)")
_UNESCAPED_MDX_LT_RE = re.compile(
    r"(?<!\\)<(?!(?:/?(?:[A-Z][A-Za-z0-9_.:-]*|(?:br|sub|sup|b|i|u|em|strong|code|span|a|p|div|kbd|del|ins|mark|small|ul|ol|li|table|thead|tbody|tr|td|th|h[1-6]|hr|img|details|summary))(?:[\s/>])|!--|https?://|mailto:))",
    re.ASCII,
)


def _sanitize_mdx_angle_brackets(text: str) -> str:
    """
    Escape bare `<` characters outside inline backtick spans that are not valid GuideMe/MDX JSX tags
    or standard HTML tags (e.g., `(<, >)` -> `(\\<, >)`), preventing Micromark MDX ParseException.
    """
    if not text or "<" not in text:
        return text
    parts = _INLINE_CODE_SPLIT_RE.split(text)
    for idx in range(0, len(parts), 2):
        if "<" in parts[idx]:
            parts[idx] = _UNESCAPED_MDX_LT_RE.sub(r"\\<", parts[idx])
    return "".join(parts)


def reconstruct_guideme_markdown(md_text: str, translated_flat: Dict[str, str], file_prefix: str = "") -> str:
    """
    Reconstruct a GuideMe `.md` guidebook file using translated `nav_title` and `b_{idx}` blocks,
    preserving YAML frontmatter, `<GameScene>`, `<Row>`, `<ItemLink>`, and code blocks 100% intact.
    """
    if not md_text:
        return md_text
    prefix = f"{file_prefix}::" if file_prefix else ""

    out_parts: List[str] = []
    body_start = 0
    fm_match = _FRONTMATTER_RE.match(md_text)
    if fm_match:
        fm_open, fm_content, fm_close = fm_match.group(1), fm_match.group(2), fm_match.group(3)
        body_start = fm_match.end()
        nav_key = f"{prefix}nav_title"
        if nav_key in translated_flat and translated_flat[nav_key].strip():
            ko_title = translated_flat[nav_key].strip()
            safe_ko_title = json.dumps(ko_title, ensure_ascii=False)
            fm_content = _FM_NAV_TITLE_RE.sub(
                lambda m: f"{m.group(1)}{safe_ko_title}{m.group(3)}",
                fm_content,
                count=1,
            )
        out_parts.append(f"{fm_open}{fm_content}{fm_close}")

    body = md_text[body_start:]
    lines = body.splitlines(keepends=True)

    in_code_fence = False
    in_game_scene = False
    current_para: List[str] = []
    block_idx = 0

    def _flush_para():
        nonlocal current_para, block_idx
        if not current_para:
            return
        raw_joined = "".join(current_para)
        trailing_nl = "\r\n" if raw_joined.endswith("\r\n") else ("\n" if raw_joined.endswith("\n") else "")
        b_key = f"{prefix}b_{block_idx}"
        block_idx += 1
        current_para = []
        if b_key in translated_flat and translated_flat[b_key].strip():
            safe_block = _sanitize_mdx_angle_brackets(translated_flat[b_key].strip())
            out_parts.append(safe_block + trailing_nl)
        else:
            out_parts.append(_sanitize_mdx_angle_brackets(raw_joined))

    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            _flush_para()
            in_code_fence = not in_code_fence
            out_parts.append(line)
            continue
        if in_code_fence:
            out_parts.append(line)
            continue

        if "<GameScene" in s:
            _flush_para()
            out_parts.append(line)
            if "</GameScene>" not in s and not s.endswith("/>"):
                in_game_scene = True
            continue
        if in_game_scene:
            out_parts.append(line)
            if "</GameScene>" in s:
                in_game_scene = False
            continue

        if not s:
            _flush_para()
            out_parts.append(line)
            continue

        if _PURE_JSX_LINE_RE.match(s):
            _flush_para()
            out_parts.append(line)
            continue

        if s.startswith("#"):
            _flush_para()
            current_para.append(line)
            _flush_para()
            continue

        if s.startswith(("- ", "* ", "|")) or re.match(r"^\d+\.\s", s):
            _flush_para()
            current_para.append(line)
            _flush_para()
            continue

        current_para.append(line)

    _flush_para()
    return "".join(out_parts)


@dataclass
class ModEntry:
    """Represents a single translatable target (a Mod namespace, FTB Quests chapter, or KubeJS namespace)."""

    entry_id: str                 # Unique ID, e.g. "mod:ae2" or "ftbquests:chapters/welcome"
    mod_id: str                   # Namespace / modid, e.g. "ae2"
    display_name: str             # Human-readable name, e.g. "Applied Energistics 2"
    version: str                  # Mod version
    source_type: str              # "mod_jar" | "ftbquests" | "kubejs"
    source_file: str              # Relative or basename of JAR / file
    en_path_in_source: str        # Path inside JAR or relative path on disk
    total_keys: int = 0
    builtin_ko_keys: int = 0      # Keys already in JAR's ko_kr.json
    cached_ko_keys: int = 0       # Keys valid & reused from our Mod Archive / cache
    version_changed_keys: int = 0 # Keys whose English text changed in a newer mod version (needs re-translation)
    untranslated_keys: int = 0    # Keys needing translation (new keys + version_changed_keys)
    unique_untranslated: int = 0  # Unique English strings needing LLM translation (after deduplication)
    total_chars: int = 0
    estimated_tokens: int = 0     # Estimated input tokens for unique untranslated strings
    raw_estimated_tokens: int = 0 # Estimated input tokens without deduplication
    total_all_tokens: int = 0     # Estimated input tokens if translating ALL keys
    has_builtin_ko: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        effective_done = min(self.total_keys, self.builtin_ko_keys + self.cached_ko_keys)
        d["progress_pct"] = round((effective_done / self.total_keys) * 100, 1) if self.total_keys > 0 else 100.0
        d["dedup_saved_keys"] = max(0, self.untranslated_keys - self.unique_untranslated)
        return d


class ModpackScanner:
    """Scans a Minecraft modpack directory for all translatable mods, FTB Quests, and KubeJS lang files."""

    def __init__(self, modpack_path: str):
        self.raw_path = os.path.abspath(modpack_path)
        self.mc_root = self._resolve_minecraft_root(self.raw_path)
        self.modpack_info: Dict[str, str] = self.detect_modpack_metadata()
        self.entries: Dict[str, ModEntry] = {}
        # In-memory cache of parsed English & built-in Korean dictionaries: entry_id -> {key: val}
        self._en_data: Dict[str, Dict[str, str]] = {}
        self._raw_original_data: Dict[str, dict] = {}
        self._builtin_ko_data: Dict[str, Dict[str, str]] = {}
        self._linked_cache_data: Dict[str, dict] = {}

    @staticmethod
    def _resolve_minecraft_root(path: str) -> str:
        """If the user points to 'FTB Evolution 2', automatically resolve 'FTB Evolution 2/minecraft'."""
        for sub in ("minecraft", ".minecraft"):
            mc_sub = os.path.join(path, sub)
            if os.path.isdir(mc_sub) and os.path.isdir(os.path.join(mc_sub, "mods")):
                return mc_sub
        return path

    @staticmethod
    def _sanitize_fs_name(val: str) -> str:
        """Sanitize a string so it is safe for folder and file names on Windows/Linux."""
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", (val or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
        return cleaned or "Modpack"

    def detect_modpack_metadata(self) -> Dict[str, str]:
        """
        Automatically detect Modpack Name, Modpack Version, and Minecraft Version from:
        - PrismLauncher / MultiMC (`instance.cfg`, `mmc-pack.json`)
        - CurseForge (`minecraftinstance.json`, `manifest.json`)
        - Better Compatibility Checker (`config/bcc-common.toml`, `config/bcc.json`)
        - Folder name fallback
        """
        base_name = os.path.basename(self.mc_root)
        instance_root = (
            os.path.dirname(self.mc_root)
            if base_name.lower() in ("minecraft", ".minecraft")
            else self.mc_root
        )

        pack_name = ""
        pack_version = ""
        mc_version = ""

        # 1. PrismLauncher / MultiMC `instance.cfg`
        for cand_dir in (instance_root, self.raw_path, self.mc_root):
            cfg_path = os.path.join(cand_dir, "instance.cfg")
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                        cfg_data: Dict[str, str] = {}
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
                                continue
                            k, v = line.split("=", 1)
                            cfg_data[k.strip()] = v.strip().strip('"').strip()
                        pack_name = cfg_data.get("ManagedPackName") or cfg_data.get("name") or pack_name
                        pack_version = cfg_data.get("ManagedPackVersionName") or cfg_data.get("ExportVersion") or pack_version
                except Exception:
                    pass
                break

        # 2. PrismLauncher / MultiMC `mmc-pack.json`
        for cand_dir in (instance_root, self.raw_path, self.mc_root):
            mmc_path = os.path.join(cand_dir, "mmc-pack.json")
            if os.path.isfile(mmc_path):
                try:
                    with open(mmc_path, "r", encoding="utf-8", errors="ignore") as f:
                        mmc_json = json.load(f)
                    for comp in mmc_json.get("components", []):
                        if comp.get("uid") == "net.minecraft":
                            mc_version = str(comp.get("version") or comp.get("cachedVersion") or "").strip()
                            break
                except Exception:
                    pass
                break

        # 3. CurseForge `minecraftinstance.json` or `manifest.json`
        for cand_dir in (self.mc_root, instance_root, self.raw_path):
            cf_path = os.path.join(cand_dir, "minecraftinstance.json")
            if os.path.isfile(cf_path):
                try:
                    with open(cf_path, "r", encoding="utf-8", errors="ignore") as f:
                        cf_json = json.load(f)
                    if not pack_name:
                        pack_name = str(cf_json.get("name") or "").strip()
                    inst_mp = cf_json.get("installedModpack") or {}
                    inst_file = inst_mp.get("installedFile") or {}
                    if not pack_version:
                        pack_version = str(inst_file.get("fileName") or inst_file.get("displayName") or "").strip()
                except Exception:
                    pass
            man_path = os.path.join(cand_dir, "manifest.json")
            if os.path.isfile(man_path):
                try:
                    with open(man_path, "r", encoding="utf-8", errors="ignore") as f:
                        man_json = json.load(f)
                    if not pack_name:
                        pack_name = str(man_json.get("name") or "").strip()
                    if not pack_version:
                        pack_version = str(man_json.get("version") or "").strip()
                    if not mc_version:
                        mc_version = str((man_json.get("minecraft") or {}).get("version") or "").strip()
                except Exception:
                    pass

        # 4. Better Compatibility Checker (`config/bcc-common.toml` or `config/bcc.json`)
        bcc_toml = os.path.join(self.mc_root, "config", "bcc-common.toml")
        if os.path.isfile(bcc_toml) and (not pack_name or not pack_version):
            try:
                with open(bcc_toml, "r", encoding="utf-8", errors="ignore") as f:
                    raw_bcc = f.read()
                m_name = re.search(r'modpackName\s*=\s*"([^"]+)"', raw_bcc)
                m_ver = re.search(r'modpackVersion\s*=\s*"([^"]+)"', raw_bcc)
                if m_name and not pack_name:
                    pack_name = m_name.group(1).strip()
                if m_ver and not pack_version:
                    pack_version = m_ver.group(1).strip()
            except Exception:
                pass

        if not pack_name:
            pack_name = os.path.basename(instance_root) or os.path.basename(self.raw_path) or "Minecraft-Modpack"

        if not pack_version:
            pack_version = "1.0.0"

        safe_name = self._sanitize_fs_name(pack_name)
        raw_ver_clean = self._sanitize_fs_name(pack_version)
        version_tag = raw_ver_clean if raw_ver_clean.lower().startswith("v") else f"v{raw_ver_clean}"
        slug_name = re.sub(r"\s+", "_", safe_name)
        slug_ver = re.sub(r"\s+", "_", version_tag)
        default_pack_title = f"{slug_name}_{slug_ver}_한글패치"

        return {
            "name": pack_name.strip(),
            "version": pack_version.strip(),
            "version_tag": version_tag,
            "mc_version": mc_version,
            "safe_name": safe_name,
            "safe_version": version_tag,
            "slug": f"{slug_name}_{slug_ver}",
            "default_pack_title": default_pack_title,
        }

    def scan_all(self, cached_counts: Optional[Dict[str, Dict[str, str]]] = None) -> List[ModEntry]:
        """Scan mods/*.jar, config/ftbquests, and kubejs/assets."""
        self.modpack_info = self.detect_modpack_metadata()
        self.entries.clear()
        self._en_data.clear()
        self._raw_original_data.clear()
        self._builtin_ko_data.clear()
        self._linked_cache_data.clear()

        cached_counts = cached_counts or {}

        self._scan_mod_jars(cached_counts)
        self._scan_kubejs(cached_counts)
        self._scan_ftbquests(cached_counts)

        # Sort by untranslated_keys descending, then display_name
        sorted_entries = sorted(
            self.entries.values(),
            key=lambda e: (e.untranslated_keys == 0, -e.untranslated_keys, e.display_name.lower()),
        )
        return sorted_entries

    def _parse_jar_metadata(self, zf: zipfile.ZipFile, jar_basename: str) -> Dict[str, Tuple[str, str]]:
        """Return mapping of {modId_lower: (displayName, version)} from neoforge.mods.toml / mods.toml / fabric.mod.json."""
        meta_map: Dict[str, Tuple[str, str]] = {}
        namelist = zf.namelist()

        for toml_candidate in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
            if toml_candidate in namelist:
                try:
                    raw = zf.read(toml_candidate).decode("utf-8", errors="ignore")
                    parsed = tomllib.loads(raw)
                    for mod_info in parsed.get("mods", []):
                        mid = str(mod_info.get("modId", "")).strip()
                        dname = str(mod_info.get("displayName", mid)).strip()
                        ver = str(mod_info.get("version", "")).strip()
                        if ver == "${file.jarVersion}":
                            ver = ""
                        if mid:
                            meta_map[mid.lower()] = (dname or mid, ver)
                except Exception:
                    # Fallback regex if TOML has syntax quirks
                    try:
                        raw = zf.read(toml_candidate).decode("utf-8", errors="ignore")
                        m_ids = re.findall(r'modId\s*=\s*"([^"]+)"', raw)
                        d_names = re.findall(r'displayName\s*=\s*"([^"]+)"', raw)
                        for idx, mid in enumerate(m_ids):
                            dname = d_names[idx] if idx < len(d_names) else mid
                            meta_map[mid.lower()] = (dname, "")
                    except Exception:
                        pass

        if "fabric.mod.json" in namelist:
            try:
                raw = zf.read("fabric.mod.json").decode("utf-8", errors="ignore")
                parsed = parse_relaxed_json(raw)
                mid = str(parsed.get("id", "")).strip()
                dname = str(parsed.get("name", mid)).strip()
                ver = str(parsed.get("version", "")).strip()
                if mid:
                    meta_map[mid.lower()] = (dname or mid, ver)
            except Exception:
                pass

        return meta_map

    _NON_GUIDE_DIRS = {
        "lang", "models", "textures", "blockstates", "shaders", "particles",
        "font", "sounds", "texts", "patchouli_books", "books", "atlases",
        "equipment", "items", "post_effect", "waypoint_style",
    }
    _LOCALE_SUBDIR_RE = re.compile(r"^_([a-z]{2}_[a-z]{2})/(.+)$", re.IGNORECASE)

    @classmethod
    def _match_guideme_asset_path(cls, asset_path: str) -> Optional[Tuple[str, str, str, str]]:
        """
        Match a GuideMe or Oracle Index guidebook asset path starting with `assets/`.
        Returns `(ns, g_root, clean_rel_path, locale_code)` where `locale_code` is `'en_us'`, `'ko_kr'`, or other locale.
        Supports:
        1. `assets/<ns>/guides/<g_ns>/<g_name>/<rel>.md` (`g_root = guides/<g_ns>/<g_name>`)
        2. `assets/<ns>/<folder>/<rel>.md` where `guide` in `folder` (`ae2guide`, `mi_guidebook`, `nodeworksguide`)
           or `folder == <ns>` (`ftbic/ftbic`, `auroral/auroral`)
        3. `assets/oracle_index/books/<book_id>/<rel>` (`.mdx` or `_meta.json`, mapped to `ns = <book_id>`, `g_root = __oracle__/<book_id>`)
        """
        norm = asset_path.replace("\\", "/").lstrip("/")
        norm_low = norm.lower()
        parts = norm.split("/")
        if len(parts) < 4 or parts[0].lower() != "assets":
            return None

        ns = parts[1]
        folder = parts[2]
        folder_low = folder.lower()

        # Case 3: Oracle Index (`assets/oracle_index/books/<book_id>/...`)
        if ns.lower() == "oracle_index" and folder_low == "books" and len(parts) >= 5:
            if not (norm_low.endswith(".mdx") or norm_low.endswith("/_meta.json")):
                return None
            book_id = parts[3]
            g_root = f"__oracle__/{book_id}"
            if len(parts) >= 7 and parts[4].lower() == "translated":
                loc_code = parts[5].lower()
                clean_rel = "/".join(parts[6:])
                return (book_id, g_root, clean_rel, loc_code)
            clean_rel = "/".join(parts[4:])
            return (book_id, g_root, clean_rel, "en_us")

        if not norm_low.endswith(".md"):
            return None

        # Case 1: `assets/<ns>/guides/<g_ns>/<g_name>/<rel>.md`
        if folder_low == "guides":
            if len(parts) < 6:
                return None
            g_root = f"{parts[2]}/{parts[3]}/{parts[4]}"
            rel_md = "/".join(parts[5:])
        # Case 2: `assets/<ns>/<folder>/<rel>.md` (`*guide*` or `<ns>/<ns>`)
        elif folder_low not in cls._NON_GUIDE_DIRS and ("guide" in folder_low or folder_low == ns.lower()):
            g_root = folder
            rel_md = "/".join(parts[3:])
        else:
            return None

        loc_m = cls._LOCALE_SUBDIR_RE.match(rel_md)
        if loc_m:
            return (ns, g_root, loc_m.group(2), loc_m.group(1).lower())
        if rel_md.startswith("_"):
            return None
        return (ns, g_root, rel_md, "en_us")

    def _scan_mod_jars(self, cached_translations: Dict[str, Dict[str, str]]) -> None:
        mods_dir = os.path.join(self.mc_root, "mods")
        if not os.path.isdir(mods_dir):
            return

        jar_files = sorted(glob.glob(os.path.join(mods_dir, "*.jar")))
        lang_pattern = re.compile(r"^assets/([^/]+)/lang/([a-zA-Z0-9_]+)\.json$", re.IGNORECASE)
        patchouli_pattern = re.compile(
            r"^(assets|data)/([^/]+)/patchouli_books/([^/]+)/([a-zA-Z0-9_]+)/(.+\.json)$",
            re.IGNORECASE,
        )

        for jar_path in jar_files:
            jar_basename = os.path.basename(jar_path)
            try:
                with zipfile.ZipFile(jar_path, "r") as zf:
                    namelist = zf.namelist()
                    # Fast filter: look for /lang/, guide .md/.mdx files, and patchouli_books .json files
                    lang_files_by_ns: Dict[str, Dict[str, str]] = {}
                    guideme_files_by_ns: Dict[str, Dict[str, Dict[str, str]]] = {}
                    patchouli_files_by_book: Dict[Tuple[str, str], Dict[str, Dict[str, str]]] = {}

                    for name in namelist:
                        name_low = name.lower()
                        if "/lang/" in name_low and name_low.endswith(".json"):
                            m = lang_pattern.match(name)
                            if m:
                                ns = m.group(1)
                                locale_code = m.group(2).lower()
                                lang_files_by_ns.setdefault(ns, {})[locale_code] = name
                        elif "/patchouli_books/" in name_low and name_low.endswith(".json"):
                            pm = patchouli_pattern.match(name)
                            if pm:
                                ns = pm.group(2)
                                book_name = pm.group(3)
                                loc_code = pm.group(4).lower()
                                rel_json = pm.group(5)
                                if loc_code in ("en_us", "ko_kr"):
                                    patchouli_files_by_book.setdefault((ns, book_name), {}).setdefault(
                                        loc_code, {}
                                    )[rel_json] = name
                        elif name_low.startswith("assets/") and (
                            name_low.endswith(".md") or name_low.endswith(".mdx") or name_low.endswith("/_meta.json")
                        ):
                            gm = self._match_guideme_asset_path(name)
                            if gm:
                                ns, g_root, clean_rel, loc_code = gm
                                if loc_code in ("en_us", "ko_kr"):
                                    guideme_files_by_ns.setdefault(ns, {}).setdefault(loc_code, {})[
                                        f"{g_root}/{clean_rel}"
                                    ] = name

                    if not lang_files_by_ns and not guideme_files_by_ns and not patchouli_files_by_book:
                        continue

                    meta_map = self._parse_jar_metadata(zf, jar_basename)

                    def _resolve_mod_meta(ns_key: str) -> Tuple[str, str]:
                        dn, vr = meta_map.get(ns_key.lower(), (None, ""))
                        if not dn:
                            if len(meta_map) == 1:
                                dn, vr = next(iter(meta_map.values()))
                            else:
                                dn = ns_key.replace("_", " ").title()
                        return dn, vr

                    # 1. Standard Mod Lang Files (`assets/<ns>/lang/en_us.json`)
                    for ns, locales in lang_files_by_ns.items():
                        if "en_us" not in locales:
                            continue

                        en_internal_path = locales["en_us"]
                        try:
                            en_raw = zf.read(en_internal_path).decode("utf-8", errors="ignore")
                            en_dict_raw = parse_relaxed_json(en_raw)
                            en_flat = flatten_lang_dict(en_dict_raw)
                        except Exception:
                            continue

                        if not en_flat:
                            continue

                        ko_flat: Dict[str, str] = {}
                        if "ko_kr" in locales:
                            try:
                                ko_raw = zf.read(locales["ko_kr"]).decode("utf-8", errors="ignore")
                                ko_flat = _filter_valid_builtin_ko(
                                    en_flat, flatten_lang_dict(parse_relaxed_json(ko_raw))
                                )
                            except Exception:
                                ko_flat = {}

                        entry_id = f"mod:{ns}"
                        if entry_id in self._en_data:
                            self._en_data[entry_id].update(en_flat)
                            self._raw_original_data[entry_id].update(en_dict_raw)
                            self._builtin_ko_data[entry_id].update(ko_flat)
                            en_flat = self._en_data[entry_id]
                            self._builtin_ko_data[entry_id] = _filter_valid_builtin_ko(
                                en_flat, self._builtin_ko_data[entry_id]
                            )
                            ko_flat = self._builtin_ko_data[entry_id]
                        else:
                            self._en_data[entry_id] = en_flat
                            self._raw_original_data[entry_id] = en_dict_raw
                            self._builtin_ko_data[entry_id] = ko_flat

                        dname, ver = _resolve_mod_meta(ns)
                        self._register_entry(
                            entry_id=entry_id,
                            mod_id=ns,
                            display_name=dname,
                            version=ver,
                            source_type="mod_jar",
                            source_file=jar_basename,
                            en_path_in_source=en_internal_path,
                            en_flat=en_flat,
                            ko_flat=ko_flat,
                            cached_map=cached_translations.get(entry_id, {}),
                        )

                    # 2. GuideMe ('G' Key Hold Guide, Markdown Guidebooks, and Oracle Index Wikis)
                    for ns, gm_locales in guideme_files_by_ns.items():
                        en_md_map = gm_locales.get("en_us", {})
                        if not en_md_map:
                            continue
                        ko_md_map = gm_locales.get("ko_kr", {})

                        gm_en_flat: Dict[str, str] = {}
                        gm_ko_flat: Dict[str, str] = {}
                        gm_raw_files: Dict[str, object] = {}

                        for rel_key, zip_member in sorted(en_md_map.items()):
                            try:
                                md_raw = zf.read(zip_member).decode("utf-8", errors="ignore")
                                if rel_key.lower().endswith(".json"):
                                    parsed_meta = parse_relaxed_json(md_raw)
                                    extracted = {
                                        f"{rel_key}::{k}": v
                                        for k, v in flatten_lang_dict(parsed_meta).items()
                                    }
                                    raw_obj: object = parsed_meta
                                else:
                                    extracted = extract_guideme_markdown_keys(md_raw, file_prefix=rel_key)
                                    raw_obj = md_raw
                                if extracted:
                                    gm_en_flat.update(extracted)
                                    gm_raw_files[rel_key] = raw_obj
                                    if rel_key in ko_md_map:
                                        ko_md_raw = zf.read(ko_md_map[rel_key]).decode("utf-8", errors="ignore")
                                        if rel_key.lower().endswith(".json"):
                                            ko_extracted = {
                                                f"{rel_key}::{k}": v
                                                for k, v in flatten_lang_dict(parse_relaxed_json(ko_md_raw)).items()
                                            }
                                        else:
                                            ko_extracted = extract_guideme_markdown_keys(ko_md_raw, file_prefix=rel_key)
                                        for k_ex, v_ex in ko_extracted.items():
                                            if k_ex in extracted and v_ex.strip():
                                                gm_ko_flat[k_ex] = v_ex
                            except Exception:
                                continue

                        if not gm_en_flat:
                            continue

                        gm_ko_flat = _filter_valid_builtin_ko(gm_en_flat, gm_ko_flat)
                        gm_entry_id = f"guideme:{ns}"
                        if gm_entry_id in self._en_data:
                            self._en_data[gm_entry_id].update(gm_en_flat)
                            self._raw_original_data[gm_entry_id].update(gm_raw_files)
                            self._builtin_ko_data[gm_entry_id].update(gm_ko_flat)
                            gm_en_flat = self._en_data[gm_entry_id]
                            self._builtin_ko_data[gm_entry_id] = _filter_valid_builtin_ko(
                                gm_en_flat, self._builtin_ko_data[gm_entry_id]
                            )
                            gm_ko_flat = self._builtin_ko_data[gm_entry_id]
                        else:
                            self._en_data[gm_entry_id] = gm_en_flat
                            self._raw_original_data[gm_entry_id] = gm_raw_files
                            self._builtin_ko_data[gm_entry_id] = gm_ko_flat

                        dname, ver = _resolve_mod_meta(ns)
                        sample_path = next(iter(en_md_map.values()))
                        self._register_entry(
                            entry_id=gm_entry_id,
                            mod_id=ns,
                            display_name=f"[GuideMe 가이드북] {dname}",
                            version=ver,
                            source_type="guideme",
                            source_file=jar_basename,
                            en_path_in_source=sample_path,
                            en_flat=gm_en_flat,
                            ko_flat=gm_ko_flat,
                            cached_map=cached_translations.get(gm_entry_id, {}),
                        )

                    # 3. Patchouli In-Game Guidebooks (`patchouli_books/<book>/en_us/**/*.json`)
                    for (ns, book_name), pt_locales in patchouli_files_by_book.items():
                        en_json_map = pt_locales.get("en_us", {})
                        if not en_json_map:
                            continue
                        ko_json_map = pt_locales.get("ko_kr", {})

                        pt_en_flat: Dict[str, str] = {}
                        pt_ko_flat: Dict[str, str] = {}
                        pt_raw_files: Dict[str, dict] = {}

                        for rel_json, zip_member in sorted(en_json_map.items()):
                            try:
                                raw_txt = zf.read(zip_member).decode("utf-8", errors="ignore")
                                parsed_json = parse_relaxed_json(raw_txt)
                                if not parsed_json:
                                    continue
                                extracted = flatten_patchouli_json(parsed_json, file_prefix=rel_json)
                                if extracted:
                                    pt_en_flat.update(extracted)
                                    pt_raw_files[rel_json] = parsed_json
                                    if rel_json in ko_json_map:
                                        ko_txt = zf.read(ko_json_map[rel_json]).decode("utf-8", errors="ignore")
                                        ko_parsed = parse_relaxed_json(ko_txt)
                                        if ko_parsed:
                                            ko_extracted = flatten_patchouli_json(ko_parsed, file_prefix=rel_json)
                                            for k_ex, v_ex in ko_extracted.items():
                                                if k_ex in extracted and v_ex.strip():
                                                    pt_ko_flat[k_ex] = v_ex
                            except Exception:
                                continue

                        if not pt_en_flat:
                            continue

                        pt_ko_flat = _filter_valid_builtin_ko(pt_en_flat, pt_ko_flat)
                        pt_entry_id = f"patchouli:{ns}/{book_name}"
                        if pt_entry_id in self._en_data:
                            self._en_data[pt_entry_id].update(pt_en_flat)
                            self._raw_original_data[pt_entry_id].update(pt_raw_files)
                            self._builtin_ko_data[pt_entry_id].update(pt_ko_flat)
                            pt_en_flat = self._en_data[pt_entry_id]
                            self._builtin_ko_data[pt_entry_id] = _filter_valid_builtin_ko(
                                pt_en_flat, self._builtin_ko_data[pt_entry_id]
                            )
                            pt_ko_flat = self._builtin_ko_data[pt_entry_id]
                        else:
                            self._en_data[pt_entry_id] = pt_en_flat
                            self._raw_original_data[pt_entry_id] = pt_raw_files
                            self._builtin_ko_data[pt_entry_id] = pt_ko_flat

                        dname, ver = _resolve_mod_meta(ns)
                        sample_path = next(iter(en_json_map.values()))
                        book_title = book_name.replace("_", " ").title()
                        self._register_entry(
                            entry_id=pt_entry_id,
                            mod_id=ns,
                            display_name=f"[Patchouli 책자] {dname} ({book_title})",
                            version=ver,
                            source_type="patchouli",
                            source_file=jar_basename,
                            en_path_in_source=sample_path,
                            en_flat=pt_en_flat,
                            ko_flat=pt_ko_flat,
                            cached_map=cached_translations.get(pt_entry_id, {}),
                        )
            except Exception:
                continue

    def _scan_kubejs(self, cached_translations: Dict[str, Dict[str, str]]) -> None:
        kubejs_assets = os.path.join(self.mc_root, "kubejs", "assets")
        if not os.path.isdir(kubejs_assets):
            return

        # 1. Standard KubeJS Lang Files (`kubejs/assets/<ns>/lang/en_us.json`)
        for en_file in sorted(glob.glob(os.path.join(kubejs_assets, "*", "lang", "en_us.json"))):
            ns = os.path.basename(os.path.dirname(os.path.dirname(en_file)))
            try:
                with open(en_file, "r", encoding="utf-8", errors="ignore") as f:
                    en_raw = parse_relaxed_json(f.read())
                en_flat = flatten_lang_dict(en_raw)
                if not en_flat:
                    continue

                ko_file = os.path.join(os.path.dirname(en_file), "ko_kr.json")
                ko_flat: Dict[str, str] = {}
                if os.path.isfile(ko_file):
                    with open(ko_file, "r", encoding="utf-8", errors="ignore") as f:
                        ko_flat = _filter_valid_builtin_ko(
                            en_flat, flatten_lang_dict(parse_relaxed_json(f.read()))
                        )

                entry_id, matched_cache = self._resolve_scoped_entry_id(
                    "kubejs", ns, en_flat, cached_translations
                )
                self._en_data[entry_id] = en_flat
                self._raw_original_data[entry_id] = en_raw
                self._builtin_ko_data[entry_id] = ko_flat

                self._register_entry(
                    entry_id=entry_id,
                    mod_id=ns,
                    display_name=f"[KubeJS] {ns}",
                    version=self.modpack_info.get("version_tag", "kubejs"),
                    source_type="kubejs",
                    source_file=os.path.relpath(en_file, self.mc_root),
                    en_path_in_source=en_file,
                    en_flat=en_flat,
                    ko_flat=ko_flat,
                    cached_map=matched_cache,
                )
            except Exception:
                continue

        # 2. KubeJS GuideMe (`.md` / `.mdx`) and Patchouli (`patchouli_books/<book>/.../*.json`) Guides & Overrides
        patchouli_pattern = re.compile(
            r"^assets/([^/]+)/patchouli_books/([^/]+)/([a-zA-Z0-9_]+)/(.+\.json)$",
            re.IGNORECASE,
        )
        guideme_by_ns: Dict[str, Dict[str, Dict[str, str]]] = {}
        patchouli_by_book: Dict[Tuple[str, str], Dict[str, Dict[str, str]]] = {}

        for root, _, files in os.walk(kubejs_assets):
            for fname in sorted(files):
                full_p = os.path.join(root, fname)
                rel_from_assets = "assets/" + os.path.relpath(full_p, kubejs_assets).replace("\\", "/")
                rel_low = rel_from_assets.lower()
                if "/patchouli_books/" in rel_low and rel_low.endswith(".json"):
                    pm = patchouli_pattern.match(rel_from_assets)
                    if pm:
                        ns, book_name, loc_code, rel_json = pm.group(1), pm.group(2), pm.group(3).lower(), pm.group(4)
                        if loc_code in ("en_us", "ko_kr"):
                            patchouli_by_book.setdefault((ns, book_name), {}).setdefault(loc_code, {})[rel_json] = full_p
                elif rel_low.endswith(".md") or rel_low.endswith(".mdx") or rel_low.endswith("/_meta.json"):
                    gm = self._match_guideme_asset_path(rel_from_assets)
                    if gm:
                        ns, g_root, clean_rel, loc_code = gm
                        if loc_code in ("en_us", "ko_kr"):
                            guideme_by_ns.setdefault(ns, {}).setdefault(loc_code, {})[f"{g_root}/{clean_rel}"] = full_p

        def _resolve_existing_display(ns_key: str) -> Tuple[str, str]:
            mod_entry = self.entries.get(f"mod:{ns_key}") or self.entries.get(f"guideme:{ns_key}")
            if mod_entry:
                clean_dn = re.sub(r"^\[.*?\]\s*", "", mod_entry.display_name).strip()
                return clean_dn or ns_key, mod_entry.version or self.modpack_info.get("version_tag", "kubejs")
            return ns_key.replace("_", " ").title(), self.modpack_info.get("version_tag", "kubejs")

        # 2a. Merge / Override KubeJS GuideMe guides into `guideme:<ns>`
        for ns, gm_locales in sorted(guideme_by_ns.items()):
            en_md_map = gm_locales.get("en_us", {})
            if not en_md_map:
                continue
            ko_md_map = gm_locales.get("ko_kr", {})
            gm_entry_id = f"guideme:{ns}"

            gm_en_flat = self._en_data.setdefault(gm_entry_id, {})
            gm_ko_flat = self._builtin_ko_data.setdefault(gm_entry_id, {})
            gm_raw_files = self._raw_original_data.setdefault(gm_entry_id, {})

            for rel_key, disk_path in sorted(en_md_map.items()):
                try:
                    with open(disk_path, "r", encoding="utf-8", errors="ignore") as f:
                        md_raw = f.read()
                    if rel_key.lower().endswith(".json"):
                        parsed_meta = parse_relaxed_json(md_raw)
                        extracted = {f"{rel_key}::{k}": v for k, v in flatten_lang_dict(parsed_meta).items()}
                        raw_obj: object = parsed_meta
                    else:
                        extracted = extract_guideme_markdown_keys(md_raw, file_prefix=rel_key)
                        raw_obj = md_raw
                    if not extracted:
                        continue
                    # Remove any stale keys from an overridden JAR file with the same rel_key
                    prefix_del = f"{rel_key}::"
                    for old_k in [k for k in gm_en_flat if k.startswith(prefix_del)]:
                        gm_en_flat.pop(old_k, None)
                    for old_k in [k for k in gm_ko_flat if k.startswith(prefix_del)]:
                        gm_ko_flat.pop(old_k, None)

                    gm_en_flat.update(extracted)
                    gm_raw_files[rel_key] = raw_obj

                    if rel_key in ko_md_map and os.path.isfile(ko_md_map[rel_key]):
                        with open(ko_md_map[rel_key], "r", encoding="utf-8", errors="ignore") as f:
                            ko_md_raw = f.read()
                        if rel_key.lower().endswith(".json"):
                            ko_extracted = {
                                f"{rel_key}::{k}": v
                                for k, v in flatten_lang_dict(parse_relaxed_json(ko_md_raw)).items()
                            }
                        else:
                            ko_extracted = extract_guideme_markdown_keys(ko_md_raw, file_prefix=rel_key)
                        for k_ex, v_ex in ko_extracted.items():
                            if k_ex in extracted and v_ex.strip():
                                gm_ko_flat[k_ex] = v_ex
                except Exception:
                    continue

            if not gm_en_flat:
                continue

            gm_ko_flat = _filter_valid_builtin_ko(gm_en_flat, gm_ko_flat)
            self._builtin_ko_data[gm_entry_id] = gm_ko_flat
            dname, ver = _resolve_existing_display(ns)
            existing_entry = self.entries.get(gm_entry_id)
            sample_path = existing_entry.en_path_in_source if existing_entry else next(iter(en_md_map.values()))
            src_file = existing_entry.source_file if existing_entry else f"kubejs/assets/{ns}"
            self._register_entry(
                entry_id=gm_entry_id,
                mod_id=ns,
                display_name=f"[GuideMe 가이드북] {dname}",
                version=ver,
                source_type="guideme",
                source_file=src_file,
                en_path_in_source=sample_path,
                en_flat=gm_en_flat,
                ko_flat=gm_ko_flat,
                cached_map=cached_translations.get(gm_entry_id, {}),
            )

        # 2b. Merge / Override KubeJS Patchouli book entries into `patchouli:<ns>/<book_name>`
        for (ns, book_name), pt_locales in sorted(patchouli_by_book.items()):
            en_json_map = pt_locales.get("en_us", {})
            if not en_json_map:
                continue
            ko_json_map = pt_locales.get("ko_kr", {})
            pt_entry_id = f"patchouli:{ns}/{book_name}"

            pt_en_flat = self._en_data.setdefault(pt_entry_id, {})
            pt_ko_flat = self._builtin_ko_data.setdefault(pt_entry_id, {})
            pt_raw_files = self._raw_original_data.setdefault(pt_entry_id, {})

            for rel_json, disk_path in sorted(en_json_map.items()):
                try:
                    with open(disk_path, "r", encoding="utf-8", errors="ignore") as f:
                        raw_txt = f.read()
                    parsed_json = parse_relaxed_json(raw_txt)
                    if not parsed_json:
                        continue
                    extracted = flatten_patchouli_json(parsed_json, file_prefix=rel_json)
                    # Always replace the raw JSON object (even if extracted is empty) so disabled JAR pages are overridden
                    prefix_del = f"{rel_json}::"
                    for old_k in [k for k in pt_en_flat if k.startswith(prefix_del)]:
                        pt_en_flat.pop(old_k, None)
                    for old_k in [k for k in pt_ko_flat if k.startswith(prefix_del)]:
                        pt_ko_flat.pop(old_k, None)

                    pt_raw_files[rel_json] = parsed_json
                    if extracted:
                        pt_en_flat.update(extracted)
                        if rel_json in ko_json_map and os.path.isfile(ko_json_map[rel_json]):
                            with open(ko_json_map[rel_json], "r", encoding="utf-8", errors="ignore") as f:
                                ko_parsed = parse_relaxed_json(f.read())
                            if ko_parsed:
                                ko_extracted = flatten_patchouli_json(ko_parsed, file_prefix=rel_json)
                                for k_ex, v_ex in ko_extracted.items():
                                    if k_ex in extracted and v_ex.strip():
                                        pt_ko_flat[k_ex] = v_ex
                except Exception:
                    continue

            if not pt_en_flat:
                continue

            pt_ko_flat = _filter_valid_builtin_ko(pt_en_flat, pt_ko_flat)
            self._builtin_ko_data[pt_entry_id] = pt_ko_flat
            dname, ver = _resolve_existing_display(ns)
            book_title = book_name.replace("_", " ").title()
            existing_entry = self.entries.get(pt_entry_id)
            sample_path = existing_entry.en_path_in_source if existing_entry else next(iter(en_json_map.values()))
            src_file = existing_entry.source_file if existing_entry else f"kubejs/assets/{ns}"
            self._register_entry(
                entry_id=pt_entry_id,
                mod_id=ns,
                display_name=f"[Patchouli 책자] {dname} ({book_title})",
                version=ver,
                source_type="patchouli",
                source_file=src_file,
                en_path_in_source=sample_path,
                en_flat=pt_en_flat,
                ko_flat=pt_ko_flat,
                cached_map=cached_translations.get(pt_entry_id, {}),
            )

    def _resolve_scoped_entry_id(
        self, prefix: str, rel_sub: str, en_flat: Dict[str, str], cached_translations: dict
    ) -> Tuple[str, dict]:
        """
        Scope modpack-specific entries (`ftbquests` or `kubejs`) by modpack name
        (`<prefix>:<modpack_slug>/<rel_sub>`) so different modpacks never overwrite each other
        in `data/mod_archive/`, while falling back to legacy `<prefix>:<rel_sub>` if that legacy
        entry already contains matching English text for this modpack.
        """
        pack_slug = re.sub(r"\s+", "_", self.modpack_info.get("safe_name", "Modpack"))
        scoped_id = f"{prefix}:{pack_slug}/{rel_sub}"
        legacy_id = f"{prefix}:{rel_sub}"

        if scoped_id in cached_translations and cached_translations[scoped_id]:
            return scoped_id, cached_translations[scoped_id]

        legacy_map = cached_translations.get(legacy_id, {})
        if legacy_map:
            for k, en_val in en_flat.items():
                if k in legacy_map:
                    c_entry = legacy_map[k]
                    s_en = c_entry[0] if (isinstance(c_entry, tuple) and len(c_entry) == 2) else ""
                    if (s_en and s_en == en_val) or (not s_en and prefix == "kubejs"):
                        return legacy_id, legacy_map

        return scoped_id, cached_translations.get(scoped_id, {})

    def _resolve_ftbquest_entry_id(
        self, rel_sub: str, en_flat: Dict[str, str], cached_translations: dict
    ) -> Tuple[str, dict]:
        return self._resolve_scoped_entry_id("ftbquests", rel_sub, en_flat, cached_translations)

    def _get_snbt_backup_path(self, disk_path: str) -> str:
        rel_from_mc = os.path.relpath(os.path.abspath(disk_path), self.mc_root)
        return os.path.join(self.mc_root, ".mct_backup", "ftbquests", rel_from_mc)

    def ensure_snbt_quest_backup(self, disk_path: str) -> None:
        """
        Save a clean original copy of a direct `.snbt` quest file to `.mct_backup/ftbquests/`
        before `export_all` overwrites it in-place with Korean text.
        """
        if not os.path.isfile(disk_path):
            return
        try:
            with open(disk_path, "r", encoding="utf-8", errors="ignore") as f:
                disk_txt = f.read()
            backup_path = self._get_snbt_backup_path(disk_path)
            if not _HANGUL_CHECK_RE.search(disk_txt) or not os.path.isfile(backup_path):
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                with open(backup_path, "w", encoding="utf-8") as bf:
                    bf.write(disk_txt)
        except Exception:
            pass

    def _get_clean_original_snbt_text(self, disk_path: str) -> str:
        """
        Return the clean, untranslated original `.snbt` quest text for `disk_path`:
        - If `disk_path` has no Hangul (fresh or updated modpack), refreshes the backup in `.mct_backup/ftbquests/`
          and returns `disk_path`'s content.
        - If `disk_path` already contains Hangul (from a prior `export_all`), reads the clean original from
          `.mct_backup/ftbquests/` if available.
        """
        try:
            with open(disk_path, "r", encoding="utf-8", errors="ignore") as f:
                disk_txt = f.read()
        except Exception:
            return ""

        backup_path = self._get_snbt_backup_path(disk_path)
        if not _HANGUL_CHECK_RE.search(disk_txt):
            try:
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                with open(backup_path, "w", encoding="utf-8") as bf:
                    bf.write(disk_txt)
            except Exception:
                pass
            return disk_txt

        if os.path.isfile(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8", errors="ignore") as bf:
                    return bf.read()
            except Exception:
                pass
        return disk_txt

    def _scan_ftbquests(self, cached_translations: Dict[str, Dict[str, str]]) -> None:
        ftb_quests_base = os.path.join(self.mc_root, "config", "ftbquests", "quests")
        if not os.path.isdir(ftb_quests_base):
            return

        ftb_lang_base = os.path.join(ftb_quests_base, "lang")
        found_lang_en = False

        if os.path.isdir(ftb_lang_base):
            # 1. Check for single-file FTB Quests language files (e.g. `lang/en_us.snbt`, `lang/en_us.json5`, `lang/en_us.json`)
            for single_ext in (".snbt", ".json5", ".json"):
                single_en = os.path.join(ftb_lang_base, f"en_us{single_ext}")
                if os.path.isfile(single_en):
                    try:
                        with open(single_en, "r", encoding="utf-8", errors="ignore") as f:
                            en_raw = parse_relaxed_json(f.read())
                        en_flat = flatten_lang_dict(en_raw)
                        if en_flat:
                            found_lang_en = True
                            single_ko = os.path.join(ftb_lang_base, f"ko_kr{single_ext}")
                            ko_flat: Dict[str, str] = {}
                            if os.path.isfile(single_ko):
                                with open(single_ko, "r", encoding="utf-8", errors="ignore") as f:
                                    ko_flat = _filter_valid_builtin_ko(
                                        en_flat, flatten_lang_dict(parse_relaxed_json(f.read()))
                                    )

                            rel_sub = f"en_us{single_ext}"
                            entry_id, matched_cache = self._resolve_ftbquest_entry_id(rel_sub, en_flat, cached_translations)
                            self._en_data[entry_id] = en_flat
                            self._raw_original_data[entry_id] = en_raw
                            self._builtin_ko_data[entry_id] = ko_flat

                            self._register_entry(
                                entry_id=entry_id,
                                mod_id="ftbquests",
                                display_name=f"[FTB Quests] 통합 퀘스트 ({rel_sub})",
                                version=self.modpack_info.get("version_tag", "Quest"),
                                source_type="ftbquests",
                                source_file=f"ftbquests/lang/{rel_sub}",
                                en_path_in_source=f"__single__/{rel_sub}",
                                en_flat=en_flat,
                                ko_flat=ko_flat,
                                cached_map=matched_cache,
                            )
                    except Exception:
                        pass

            # 2. Check for directory-based FTB Quests language files (`lang/en_us/**/*.json5`, `.json`, `.snbt`)
            ftb_en_dir = os.path.join(ftb_lang_base, "en_us")
            if os.path.isdir(ftb_en_dir):
                ftb_ko_dir = os.path.join(ftb_lang_base, "ko_kr")

                for root, _, files in os.walk(ftb_en_dir):
                    for fname in sorted(files):
                        if not (fname.endswith(".json5") or fname.endswith(".json") or fname.endswith(".snbt")):
                            continue
                        full_en = os.path.join(root, fname)
                        rel_sub = os.path.relpath(full_en, ftb_en_dir).replace("\\", "/")
                        try:
                            with open(full_en, "r", encoding="utf-8", errors="ignore") as f:
                                en_raw = parse_relaxed_json(f.read())
                            en_flat = flatten_lang_dict(en_raw)
                            if not en_flat:
                                continue
                            found_lang_en = True

                            full_ko = os.path.join(ftb_ko_dir, rel_sub)
                            ko_flat = {}
                            if os.path.isfile(full_ko):
                                with open(full_ko, "r", encoding="utf-8", errors="ignore") as f:
                                    ko_flat = _filter_valid_builtin_ko(
                                        en_flat, flatten_lang_dict(parse_relaxed_json(f.read()))
                                    )

                            clean_name = os.path.splitext(rel_sub)[0].replace("chapters/", "Chapter: ").replace("_", " ").title()
                            entry_id, matched_cache = self._resolve_ftbquest_entry_id(rel_sub, en_flat, cached_translations)
                            self._en_data[entry_id] = en_flat
                            self._raw_original_data[entry_id] = en_raw
                            self._builtin_ko_data[entry_id] = ko_flat

                            self._register_entry(
                                entry_id=entry_id,
                                mod_id="ftbquests",
                                display_name=f"[FTB Quests] {clean_name}",
                                version=self.modpack_info.get("version_tag", "Quest"),
                                source_type="ftbquests",
                                source_file=f"ftbquests/lang/en_us/{rel_sub}",
                                en_path_in_source=rel_sub,
                                en_flat=en_flat,
                                ko_flat=ko_flat,
                                cached_map=matched_cache,
                            )
                        except Exception:
                            continue

        if found_lang_en:
            return

        # 3. Direct SNBT Quest Files (MC 1.20.1 / FTB Quests without `lang/en_us`, e.g. Monifactory, ATM9):
        #    Scans `config/ftbquests/quests/**/*.snbt` (and `config-overrides/*/ftbquests/quests/**/*.snbt`),
        #    resolving `{key}` references from `kubejs/assets/*/lang/en_us.json` as well as inline strings.
        ref_en_lookup: Dict[str, str] = {}
        ref_ko_lookup: Dict[str, str] = {}
        ref_cached_map: dict = {}

        # Gather reference dictionaries from KubeJS entries (and mod_jar fallback)
        for eid, ent in sorted(
            self.entries.items(),
            key=lambda item: (0 if item[1].source_type == "mod_jar" else 1, item[0]),
        ):
            if ent.source_type in ("mod_jar", "kubejs"):
                ref_en_lookup.update(self._en_data.get(eid, {}))
                ref_ko_lookup.update(self._builtin_ko_data.get(eid, {}))
                ref_cached_map.update(cached_translations.get(eid, {}))

        config_overrides_base = os.path.join(self.mc_root, "config-overrides")
        has_overrides = os.path.isdir(config_overrides_base)

        for root, dirs, files in os.walk(ftb_quests_base):
            # Do not walk into `lang/` or backup directories
            dirs[:] = [d for d in sorted(dirs) if d.lower() not in ("lang", ".mct_backup")]
            for fname in sorted(files):
                if not fname.endswith(".snbt"):
                    continue
                full_snbt = os.path.join(root, fname)
                rel_sub = os.path.relpath(full_snbt, ftb_quests_base).replace("\\", "/")
                try:
                    orig_snbt_text = self._get_clean_original_snbt_text(full_snbt)
                    if not orig_snbt_text:
                        continue
                    en_flat = extract_snbt_quest_keys(orig_snbt_text, ref_en_lookup)

                    override_entries: List[dict] = []
                    if has_overrides:
                        ov_pattern = os.path.join(
                            config_overrides_base, "*", "ftbquests", "quests", rel_sub.replace("/", os.sep)
                        )
                        for idx, ov_p in enumerate(sorted(glob.glob(ov_pattern))):
                            if not os.path.isfile(ov_p):
                                continue
                            ov_orig = self._get_clean_original_snbt_text(ov_p)
                            if not ov_orig:
                                continue
                            ov_prefix = f"ov_{idx}_"
                            ov_keys = extract_snbt_quest_keys(ov_orig, ref_en_lookup, file_prefix=ov_prefix)
                            for k_ov, v_ov in ov_keys.items():
                                if not k_ov.startswith("__inline__"):
                                    en_flat.setdefault(k_ov, v_ov)
                                elif v_ov not in en_flat.values():
                                    en_flat[k_ov] = v_ov
                            override_entries.append(
                                {
                                    "rel_mc_path": os.path.relpath(ov_p, self.mc_root).replace("\\", "/"),
                                    "orig_text": ov_orig,
                                    "file_prefix": ov_prefix,
                                }
                            )

                    if not en_flat:
                        continue

                    ko_flat = _filter_valid_builtin_ko(
                        en_flat,
                        {k: ref_ko_lookup[k] for k in en_flat if k in ref_ko_lookup and ref_ko_lookup[k].strip()},
                    )

                    clean_name = (
                        os.path.splitext(rel_sub)[0]
                        .replace("chapters/", "Chapter: ")
                        .replace("reward_tables/", "Reward Table: ")
                        .replace("_", " ")
                        .title()
                    )
                    entry_id, matched_cache = self._resolve_ftbquest_entry_id(rel_sub, en_flat, cached_translations)

                    linked_cache = {k: ref_cached_map[k] for k in en_flat if k in ref_cached_map}
                    effective_cache = dict(linked_cache)
                    effective_cache.update(matched_cache)

                    self._en_data[entry_id] = en_flat
                    self._builtin_ko_data[entry_id] = ko_flat
                    self._linked_cache_data[entry_id] = linked_cache
                    self._raw_original_data[entry_id] = {
                        "__snbt_direct__": True,
                        "rel_quest_path": f"config/ftbquests/quests/{rel_sub}",
                        "orig_text": orig_snbt_text,
                        "overrides": override_entries,
                    }

                    self._register_entry(
                        entry_id=entry_id,
                        mod_id="ftbquests",
                        display_name=f"[FTB Quests] {clean_name}",
                        version=self.modpack_info.get("version_tag", "Quest"),
                        source_type="ftbquests",
                        source_file=f"config/ftbquests/quests/{rel_sub}",
                        en_path_in_source=f"__snbt_direct__/{rel_sub}",
                        en_flat=en_flat,
                        ko_flat=ko_flat,
                        cached_map=effective_cache,
                    )
                except Exception:
                    continue

    def _register_entry(
        self,
        entry_id: str,
        mod_id: str,
        display_name: str,
        version: str,
        source_type: str,
        source_file: str,
        en_path_in_source: str,
        en_flat: Dict[str, str],
        ko_flat: Dict[str, str],
        cached_map: dict,
    ) -> None:
        total_keys = len(en_flat)
        builtin_ko_count = 0
        cached_ko_count = 0
        version_changed_count = 0
        untranslated_count = 0
        untranslated_chars = 0
        raw_untranslated_tokens = 0
        dedup_untranslated_tokens = 0
        total_all_tokens = 0
        seen_untranslated_vals = set()

        alpha_re = re.compile(r"[a-zA-Z]{2,}")
        tag_strip_re = re.compile(r"\{(?:@[a-zA-Z0-9_]+|image:[^}]+)\}")

        # Build an in-mod EN -> KO lookup from cached_map so renamed keys in a newer version of the same mod
        # still reuse the mod's existing translation if the English sentence is identical.
        mod_en_to_ko: Dict[str, str] = {}
        for _, c_val in cached_map.items():
            if isinstance(c_val, tuple) and len(c_val) == 2:
                s_en, s_ko = c_val
                if s_en and s_ko and s_ko.strip():
                    mod_en_to_ko[s_en] = s_ko

        for k, en_val in en_flat.items():
            # We send compact numeric index keys ("0": "text") to the LLM, so key overhead is only ~3 tokens
            pair_chars = len(en_val) + 8
            pair_tokens = estimate_tokens(en_val) + 3
            total_all_tokens += pair_tokens

            # Pure image tags ({image:...}), pagebreaks ({@pagebreak}), or numeric/symbol strings need no translation
            is_non_translatable = not alpha_re.search(tag_strip_re.sub("", en_val))
            in_builtin = (k in ko_flat and bool(ko_flat[k].strip())) or is_non_translatable
            in_cache = False

            if not in_builtin:
                if k in cached_map:
                    c_entry = cached_map[k]
                    if isinstance(c_entry, tuple) and len(c_entry) == 2:
                        saved_en, saved_ko = c_entry
                        if saved_ko and saved_ko.strip():
                            # Version-aware check: if the English text is unchanged, reuse it!
                            if saved_en == en_val or (not saved_en and source_type != "ftbquests"):
                                in_cache = True
                            elif en_val in mod_en_to_ko:
                                in_cache = True
                            else:
                                # The key exists in our archive, but the new mod version changed its English text!
                                version_changed_count += 1
                                in_cache = False
                    elif isinstance(c_entry, str) and c_entry.strip() and source_type != "ftbquests":
                        in_cache = True
                elif en_val in mod_en_to_ko:
                    # Key was renamed in the new mod version, but English text is identical in this mod's archive
                    in_cache = True

            if in_builtin:
                builtin_ko_count += 1
            if in_cache and not in_builtin:
                cached_ko_count += 1

            if not in_builtin and not in_cache:
                untranslated_count += 1
                untranslated_chars += pair_chars
                raw_untranslated_tokens += pair_tokens
                if en_val not in seen_untranslated_vals:
                    seen_untranslated_vals.add(en_val)
                    dedup_untranslated_tokens += pair_tokens

        self.entries[entry_id] = ModEntry(
            entry_id=entry_id,
            mod_id=mod_id,
            display_name=display_name,
            version=version,
            source_type=source_type,
            source_file=source_file,
            en_path_in_source=en_path_in_source,
            total_keys=total_keys,
            builtin_ko_keys=builtin_ko_count,
            cached_ko_keys=cached_ko_count,
            version_changed_keys=version_changed_count,
            untranslated_keys=untranslated_count,
            unique_untranslated=len(seen_untranslated_vals),
            total_chars=untranslated_chars,
            estimated_tokens=dedup_untranslated_tokens,
            raw_estimated_tokens=raw_untranslated_tokens,
            total_all_tokens=total_all_tokens,
            has_builtin_ko=builtin_ko_count > 0,
        )

    def get_en_dict(self, entry_id: str) -> Dict[str, str]:
        return self._en_data.get(entry_id, {})

    def get_builtin_ko_dict(self, entry_id: str) -> Dict[str, str]:
        return self._builtin_ko_data.get(entry_id, {})

    def get_raw_original_data(self, entry_id: str) -> dict:
        return self._raw_original_data.get(entry_id, {})

    def get_linked_cache_dict(self, entry_id: str) -> dict:
        return self._linked_cache_data.get(entry_id, {})

    def extract_builtin_tm_pairs(self) -> Dict[str, str]:
        """Extract verified EN -> KO string pairs from mods that already have built-in ko_kr.json."""
        from .validator import extract_placeholders

        tm: Dict[str, str] = {}
        alpha_re = re.compile(r"[a-zA-Z]{2,}")
        for entry_id, ko_dict in self._builtin_ko_data.items():
            en_dict = self._en_data.get(entry_id, {})
            for k, ko_val in ko_dict.items():
                en_val = en_dict.get(k, "")
                if not en_val or not ko_val or en_val.strip() == ko_val.strip():
                    continue
                if not alpha_re.search(en_val):
                    continue
                if extract_placeholders(en_val) != extract_placeholders(ko_val):
                    continue
                tm[en_val] = ko_val
        return tm


