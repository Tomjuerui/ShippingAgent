"""OCR 纠错与单证号校验（FR-14 V4/V6 / WP-05）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import product

_ISO6346_LETTER_VALUES = {
    "A": 10, "B": 12, "C": 13, "D": 14, "E": 15, "F": 16, "G": 17, "H": 18,
    "I": 19, "J": 20, "K": 21, "L": 23, "M": 24, "N": 25, "O": 26, "P": 27,
    "Q": 28, "R": 29, "S": 30, "T": 31, "U": 32, "V": 34, "W": 35, "X": 36,
    "Y": 37, "Z": 38,
}
_LETTER_FIX = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
_DIGIT_FIX = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
_BOX_PATTERN = re.compile(r"^[A-Z]{4}\d{7}$")
_BL_PATTERN = re.compile(r"^[A-Z]{4}\d{7,11}$")


@dataclass
class FieldFix:
    value: str | None = None
    repaired: bool = False
    valid: bool = True
    notes: list[str] = field(default_factory=list)


def clean_reference(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    return re.sub(r"\s+", "", str(value).strip()).upper()


def iso6346_check_digit(container: str) -> int | None:
    code = (container or "").upper()
    if len(code) != 11:
        return None
    total = 0
    for position, char in enumerate(code[:10]):
        if char.isdigit():
            number = int(char)
        elif char in _ISO6346_LETTER_VALUES:
            number = _ISO6346_LETTER_VALUES[char]
        else:
            return None
        total += number * (2**position)
    remainder = total % 11
    return 0 if remainder == 10 else remainder


def _variants(code: str, letter_positions: range, digit_positions: range, limit: int = 512):
    options: list[list[str]] = []
    for index, char in enumerate(code):
        choices = [char]
        if index in letter_positions and char.isdigit() and char in _LETTER_FIX:
            choices.append(_LETTER_FIX[char])
        elif index in digit_positions and char.isalpha() and char in _DIGIT_FIX:
            choices.append(_DIGIT_FIX[char])
        options.append(choices)
    for combo in product(*options):
        yield "".join(combo)


def normalize_container(raw: str | None) -> FieldFix:
    text = str(raw or "").strip()
    parts = [part for part in re.split(r"[,;/、\s]+", text) if part.strip()]
    if len(parts) > 1:
        fixes = [normalize_container(part) for part in parts]
        valid = [fix for fix in fixes if fix.value and fix.valid]
        if valid:
            chosen = valid[0]
            return FieldFix(
                chosen.value,
                repaired=any(fix.repaired for fix in fixes),
                notes=[
                    f"原文包含多个箱号：{text}；匹配使用第一个有效箱号 {chosen.value}"
                ],
            )
        return FieldFix(
            clean_reference(text),
            valid=False,
            notes=[f"多个箱号均未通过 ISO 6346 校验：{text}"],
        )
    value = clean_reference(raw)
    if not value:
        return FieldFix(None, notes=["箱号缺失"])
    value = value.replace("-", "")
    if _BOX_PATTERN.match(value):
        expected = iso6346_check_digit(value)
        if expected is not None and int(value[-1]) == expected:
            return FieldFix(value)
        candidates = [
            candidate
            for candidate in _variants(value, range(0, 4), range(4, 11))
            if _BOX_PATTERN.match(candidate)
            and iso6346_check_digit(candidate) is not None
            and int(candidate[-1]) == iso6346_check_digit(candidate)
        ]
        candidates = sorted(set(candidates))
        if len(candidates) == 1 and candidates[0] != value:
            return FieldFix(
                candidates[0],
                repaired=True,
                notes=[f"箱号校验位纠错：{value} → {candidates[0]}"],
            )
        return FieldFix(
            value,
            valid=False,
            notes=[f"箱号 {value} 未通过 ISO 6346 校验位"],
        )
    candidates = [
        candidate
        for candidate in _variants(
            value, range(0, min(4, len(value))), range(4, len(value))
        )
        if _BOX_PATTERN.match(candidate)
        and iso6346_check_digit(candidate) is not None
        and int(candidate[-1]) == iso6346_check_digit(candidate)
    ]
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return FieldFix(candidates[0], repaired=True, notes=[f"箱号 OCR 纠错：{raw} → {candidates[0]}"])
    return FieldFix(value, valid=False, notes=[f"箱号格式不符合 ISO 6346：{raw}"])


def normalize_bl_no(raw: str | None) -> FieldFix:
    value = clean_reference(raw)
    if not value:
        return FieldFix(None, notes=["提单号缺失"])
    value = value.replace("-", "")
    if _BL_PATTERN.match(value):
        return FieldFix(value)
    candidates = [
        candidate
        for candidate in _variants(value, range(0, 4), range(4, len(value)))
        if _BL_PATTERN.match(candidate)
    ]
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return FieldFix(
            candidates[0], repaired=True, notes=[f"提单号 OCR 纠错：{raw} → {candidates[0]}"]
        )
    return FieldFix(value, valid=True, notes=["提单号未匹配标准模式，保留原值，未强行纠错"])


def normalize_booking_no(raw: str | None) -> FieldFix:
    value = clean_reference(raw)
    return FieldFix(value, notes=[] if value else ["订舱号缺失"])


def normalize_date(raw: str | None, today: date | None = None) -> FieldFix:
    today = today or date.today()
    if not raw or not str(raw).strip():
        return FieldFix(None, notes=["单据日期缺失"])
    text = str(raw).strip()
    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
        "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%m.%d.%Y",
        "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y",
    )
    parsed: datetime | None = None
    used: str | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            used = fmt
            break
        except ValueError:
            continue
    if parsed is None:
        return FieldFix(None, valid=False, notes=[f"日期格式无法解析：{raw}"])
    value = parsed.date()
    notes: list[str] = []
    repaired = False
    if used in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%m.%d.%Y"):
        parts = [part for part in re.split(r"\D+", text) if part]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            first, second = int(parts[0]), int(parts[1])
            if first <= 12 and second <= 12:
                order = "month-first" if used in ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y") else "day-first"
                notes.append(
                    f"日期日月顺序存在歧义，按 {order} 解析：{raw} → {value.isoformat()}"
                )
        repaired = True
    if value.year < 1990 or value > today:
        return FieldFix(
            value.isoformat(),
            valid=False,
            repaired=True,
            notes=[f"日期超出合理范围（1990 ~ 今天）：{value.isoformat()}"],
        )
    if value.isoformat() != text:
        repaired = True
    return FieldFix(value.isoformat(), repaired=repaired, notes=notes)


def dedupe_text_key(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())
