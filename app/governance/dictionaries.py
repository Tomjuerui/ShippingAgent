"""字典装载与查询（WP-05 / D4）。所有字典可配置扩充，代码不硬编码映射。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml
from rapidfuzz import fuzz, process

from app.config.settings import get_settings

_PUNCT = re.compile(r"[\s\-_/\\.,:;'\"()\[\]{}&#+*%$￥€£¥]+")


def normalize_key(value: str | None) -> str:
    """费用名/编号归一：NFKC + 小写 + 去空白标点。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return _PUNCT.sub("", text)


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass(frozen=True)
class FeeMatch:
    standard: str | None
    method: str  # exact | normalized | alias | fuzzy | none
    score: float = 0.0


class FeeDictionary:
    def __init__(self, data: dict):
        self.version: str = str(data.get("version", ""))
        self._canonical: dict[str, str] = {}
        self._normalized: dict[str, str] = {}
        self._aliases: list[tuple[str, str]] = []
        for entry in data.get("canonical", []):
            name = entry.get("name")
            if not name:
                continue
            self._canonical[name.lower()] = name
            self._normalized[normalize_key(name)] = name
            for alias in entry.get("aliases", []):
                if not alias:
                    continue
                self._canonical.setdefault(alias.lower(), name)
                self._normalized.setdefault(normalize_key(alias), name)
                self._aliases.append((normalize_key(alias), name))

    def match(self, original: str | None, fuzzy_threshold: int = 92) -> FeeMatch:
        if not original or not str(original).strip():
            return FeeMatch(None, "none")
        raw = str(original).strip()
        if raw.lower() in self._canonical:
            return FeeMatch(self._canonical[raw.lower()], "exact", 100.0)
        key = normalize_key(raw)
        if key in self._normalized:
            method = "normalized" if self._normalized[key].lower() == raw.lower() else "alias"
            return FeeMatch(self._normalized[key], method, 100.0)
        if len(key) >= 4:
            choices = [alias for alias, _ in self._aliases if len(alias) >= 4]
            matches = process.extract(key, choices, scorer=fuzz.ratio, limit=2)
            if matches:
                best_alias, best_score = matches[0][0], matches[0][1]
                second = matches[1][1] if len(matches) > 1 else 0
                # 唯一且明显领先才接受，防止 "PORT FEE" 之类被模糊吞并
                if best_score >= fuzzy_threshold and best_score - second >= 3:
                    standard = next(std for alias, std in self._aliases if alias == best_alias)
                    return FeeMatch(standard, "fuzzy", float(best_score))
        return FeeMatch(None, "none")


class CurrencyDictionary:
    def __init__(self, data: dict):
        self.version: str = str(data.get("version", ""))
        self.iso: set[str] = {str(code).upper() for code in data.get("iso4217", [])}
        self._literal: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        for code, aliases in (data.get("aliases") or {}).items():
            code = str(code).upper()
            self._literal[code] = code
            self._aliases[normalize_key(code)] = code
            for alias in aliases or []:
                literal = unicodedata.normalize("NFKC", str(alias)).strip().upper()
                if literal:
                    self._literal[literal] = code
                key = normalize_key(alias)
                if key:
                    self._aliases[key] = code

    def normalize(self, raw: str | None) -> tuple[str | None, bool]:
        """返回 (ISO 代码 | None, 是否经过 OCR 纠错)。未知币种不强行映射。"""
        if not raw or not str(raw).strip():
            return None, False
        text = unicodedata.normalize("NFKC", str(raw)).strip()
        upper = text.upper()
        if upper in self._literal:
            return self._literal[upper], False
        # 抽取 3 位字母代码（如 "USD 40.00"）
        letters = re.findall(r"[A-Z]{3}", upper)
        for code in letters:
            if code in self.iso:
                return code, False
        key = normalize_key(text)
        if key and key in self._aliases:
            return self._aliases[key], False
        # OCR 距离-1 纠错（USO→USD；别名表外值不强行映射）
        candidates = list(self.iso)
        if letters and len(letters[0]) == 3:
            best = process.extractOne(letters[0], candidates, scorer=fuzz.ratio)
            if best and best[1] >= 66 and _edit_distance_le1(letters[0], best[0]):
                return best[0], True
        return None, False


def _edit_distance_le1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


class PaymentTermDictionary:
    def __init__(self, data: dict):
        self.version: str = str(data.get("version", ""))
        self._map: dict[str, str] = {}
        for code, aliases in (data.get("canonical") or {}).items():
            self._map[normalize_key(code)] = str(code).upper()
            for alias in aliases or []:
                self._map[normalize_key(alias)] = str(code).upper()

    def normalize(self, raw: str | None) -> str | None:
        if not raw:
            return None
        return self._map.get(normalize_key(str(raw)))


class EntityNormalizer:
    def __init__(self, data: dict):
        self.version: str = str(data.get("version", ""))
        self.suffixes: list[str] = [
            str(s).lower().strip() for s in data.get("legal_suffixes", [])
        ]
        self.suffixes.sort(key=len, reverse=True)

    def normalize(self, name: str | None) -> str:
        if not name:
            return ""
        text = unicodedata.normalize("NFKC", str(name)).strip().lower()
        changed = True
        while changed:
            changed = False
            for suffix in self.suffixes:
                if not text.endswith(suffix):
                    continue
                stem = text[: -len(suffix)]
                # 短后缀必须处于词边界，避免 "Cosa" 被剥成 "Co"
                boundary_ok = len(suffix) >= 5 or not stem or not stem[-1].isalnum()
                if boundary_ok and len(stem.strip(" .,-")) >= 2:
                    text = stem.strip(" .,-")
                    changed = True
                    break
        return "".join(ch for ch in text if ch.isalnum())


class DictionaryBundle:
    def __init__(self, dicts_dir: Path):
        self.fee = FeeDictionary(_read_yaml(dicts_dir / "fee_alias.yaml"))
        self.currency = CurrencyDictionary(_read_yaml(dicts_dir / "currency_alias.yaml"))
        self.payment = PaymentTermDictionary(_read_yaml(dicts_dir / "payment_term.yaml"))
        self.entity = EntityNormalizer(_read_yaml(dicts_dir / "entity_alias.yaml"))

    @property
    def version(self) -> str:
        return "|".join(
            [self.fee.version, self.currency.version, self.payment.version, self.entity.version]
        )


@lru_cache(maxsize=4)
def load_dicts(dicts_dir: str | None = None) -> DictionaryBundle:
    path = Path(dicts_dir) if dicts_dir else get_settings().dicts_dir
    return DictionaryBundle(path)


_GENERIC_NORMALIZED = {
    "agent", "agents", "shipping", "logistics", "logistic", "line", "lines",
    "company", "group", "service", "services", "trading", "international",
    "global", "forwarder", "freight", "transport", "transportation", "limited",
    "ltd", "inc", "corp", "corporation", "holdings", "enterprise", "enterprises",
}


def resolve_role(
    party: str | None,
    entity: str,
    aliases: Iterable[str] = (),
    threshold: int = 85,
) -> bool:
    """记账主体是否 ≈ 交易一方：别名表 + 双向包含 + 相似度兜底（D1 第一步）。"""
    normalizer = load_dicts().entity
    targets = [normalizer.normalize(entity)] + [normalizer.normalize(a) for a in aliases]
    p = normalizer.normalize(party)
    if not p or p in _GENERIC_NORMALIZED:
        return False
    return any(
        t
        and len(t) >= 4
        and t not in _GENERIC_NORMALIZED
        and (
            p == t
            or (len(p) >= 4 and (t in p or p in t))
            or (len(p) >= 4 and fuzz.partial_ratio(p, t) >= threshold)
        )
        for t in targets
    )
