"""Format String & Minecraft Color Code Validator and Auto-Repair."""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple


# Matches %s, %d, %f, %1$s, %2$d, %.2f, %%
PRINTF_PATTERN = re.compile(r"%(?:(\d+)\$)?[-#+ 0,(]*\d*(?:\.\d+)?[sdfeEgGxXosc%]")
# Matches {0}, {1}, {value}, {@pagebreak}, {image:...}
BRACE_PATTERN = re.compile(r"\{(?:@[0-9a-zA-Z_]+|image:[^}]+|[0-9a-zA-Z_]+)\}")
# Matches §a, §6, &a, &r etc.
MC_COLOR_PREFIX_PATTERN = re.compile(r"^((?:[§&][0-9a-fk-orA-FK-OR])+)")


def extract_placeholders(text: str) -> Tuple[List[str], List[str]]:
    """Return (printf_tokens, brace_tokens) found in text."""
    printf_tokens = [m.group(0) for m in PRINTF_PATTERN.finditer(text)]
    brace_tokens = [m.group(0) for m in BRACE_PATTERN.finditer(text)]
    return printf_tokens, brace_tokens


def validate_and_repair_translation(en_text: str, ko_text: str) -> Tuple[str, List[str]]:
    """
    Validate a translated string against its English original and auto-repair common issues.
    Returns (repaired_ko_text, list_of_warnings).
    """
    warnings: List[str] = []
    if not ko_text or not ko_text.strip():
        return en_text, ["빈 번역 결과가 반환되어 원문으로 대체됨"]

    repaired = ko_text.strip()

    # 1. Preserve leading color/formatting prefix (e.g., "&aUseful Commands" -> "&a유용한 명령어")
    en_prefix_match = MC_COLOR_PREFIX_PATTERN.match(en_text)
    if en_prefix_match:
        prefix = en_prefix_match.group(1)
        if not repaired.startswith(prefix):
            # Check if LLM stripped leading color code completely
            if not MC_COLOR_PREFIX_PATTERN.match(repaired):
                repaired = prefix + repaired
                warnings.append(f"누락된 선행 색상 코드({prefix}) 자동 복구")

    # 2. Check printf placeholders (%s, %1$s, %d)
    en_printf, en_braces = extract_placeholders(en_text)
    ko_printf, ko_braces = extract_placeholders(repaired)

    # Convert positional/non-positional comparison
    if sorted(en_printf) != sorted(ko_printf):
        warnings.append(f"포맷 스트링 불일치 감지: 원문={en_printf}, 번역={ko_printf}")
        # Auto-repair: if single %s or %1$s is missing at the end or colon, append it
        missing = list(en_printf)
        for tok in ko_printf:
            if tok in missing:
                missing.remove(tok)
        if missing:
            # If the English string ended with the placeholder (e.g. "Level: %s"), append missing placeholder
            for m_tok in missing:
                if en_text.rstrip().endswith(m_tok) and m_tok not in repaired:
                    repaired = f"{repaired} {m_tok}"
                    warnings.append(f"누락된 포맷 스트링({m_tok}) 후미 자동 복구")

    # 3. Check brace placeholders ({0}, {1})
    if sorted(en_braces) != sorted(ko_braces):
        warnings.append(f"중괄호 홀더 불일치 감지: 원문={en_braces}, 번역={ko_braces}")
        missing_b = [b for b in en_braces if b not in ko_braces]
        for mb in missing_b:
            if en_text.rstrip().endswith(mb) and mb not in repaired:
                repaired = f"{repaired} {mb}"
                warnings.append(f"누락된 홀더({mb}) 자동 복구")

    # 4. If en_text is a serialized JSON array (like FTB Quests rich text component ["", "Press ", {...}]),
    # verify that repaired is also valid JSON if it starts with '['
    if en_text.strip().startswith("[") and en_text.strip().endswith("]"):
        try:
            json.loads(en_text)
            en_is_json_arr = True
        except Exception:
            en_is_json_arr = False

        if en_is_json_arr:
            try:
                json.loads(repaired)
            except Exception:
                warnings.append("FTB Quests JSON 컴포넌트 문법 손상 감지 -> 안전을 위해 원문 구조 유지")
                repaired = en_text

    return repaired, warnings
