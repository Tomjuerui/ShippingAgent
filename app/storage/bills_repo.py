"""已有账单仓储（FR-09 / D10 / WP-07）。

兼容扩展格式（6.2，含 shipment_info / charges[]）与《要求.md》第五节最小格式；
最小格式加载期做派生键重建（reference_no / remarks / audit_trail 文本挖掘）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config.settings import Settings
from app.extraction.schemas import (
    ChargeDetailsOut,
    DocumentInfoOut,
    ExistingBill,
    FinancialAttributesOut,
    MatchKeyCoverage,
    ShipmentInfoOut,
    TransactionPartyOut,
)
from app.governance.dictionaries import load_dicts, normalize_key
from app.governance.ocr_fix import (
    clean_reference,
    iso6346_check_digit,
    normalize_date,
)

_CONTAINER_RE = re.compile(r"\b[A-Z]{4}\d{7}\b")
_BL_RE = re.compile(r"\b[A-Z]{4}\d{7,11}\b")
_BOOKING_RE = re.compile(r"\b(?:BK|BKG|BOOKING)[-\s]?[A-Z0-9][A-Z0-9-]{2,}\b", re.IGNORECASE)
_EXCLUDE_REF = re.compile(r"^(DN|CN|INV|INVOICE|DEBIT|CREDIT)", re.IGNORECASE)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _classify_token(token: str) -> str | None:
    token = token.upper()
    if _CONTAINER_RE.fullmatch(token) and iso6346_check_digit(token) is not None:
        if int(token[-1]) == iso6346_check_digit(token):
            return "container_no"
    if _BL_RE.fullmatch(token) and not _EXCLUDE_REF.match(token):
        return "bl_no"
    return None


def _extract_numbers(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in re.findall(r"\b[A-Z]{4}\d{7,11}\b", text.upper()):
        kind = _classify_token(token)
        if kind and kind not in found:
            found[kind] = token
    booking = _BOOKING_RE.search(text)
    if booking and "booking_no" not in found:
        found["booking_no"] = re.sub(r"\s+", "", booking.group(0)).upper()
    return found


def _derive_keys(
    reference_no: str | None,
    raw_text: str,
    shipment: ShipmentInfoOut,
) -> dict[str, bool]:
    derived: dict[str, bool] = {}
    if reference_no:
        ref = clean_reference(reference_no) or ""
        kind = _classify_token(ref)
        if kind and not getattr(shipment, kind):
            setattr(shipment, kind, ref)
            derived[kind] = True
        elif _BOOKING_RE.fullmatch(ref) and not shipment.booking_no:
            shipment.booking_no = ref
            derived["booking_no"] = True
    mined = _extract_numbers(raw_text)
    for kind, value in mined.items():
        if not getattr(shipment, kind):
            setattr(shipment, kind, value)
            derived[kind] = True
    return derived


def _normalize_existing_charge(data: dict) -> ChargeDetailsOut:
    dicts = load_dicts()
    original = data.get("original_charge_name")
    standard = data.get("charge_item")
    if not standard and original:
        standard = dicts.fee.match(original).standard
    currency, _ = dicts.currency.normalize(data.get("currency"))
    payment = dicts.payment.normalize(data.get("payment_term")) or data.get("payment_term")
    return ChargeDetailsOut(
        original_charge_name=original,
        charge_item=standard,
        quantity=data.get("quantity"),
        unit_price=data.get("unit_price"),
        amount=data.get("amount"),
        currency=currency or data.get("currency"),
        payment_term=payment,
    )


def _bill_from_extended(
    data: dict, file_path: Path, file_index: int, charge_index: int, settings: Settings
) -> ExistingBill:
    reference = (data.get("document_info") or {}).get("reference_no")
    date_raw = (data.get("document_info") or {}).get("document_date")
    date_value = normalize_date(date_raw).value or date_raw
    shipment = ShipmentInfoOut(**(data.get("shipment_info") or {}))
    derived: dict[str, bool] = {}
    if settings.matching.derived_keys_enabled:
        derived = _derive_keys(reference, _stringify(data), shipment)
    bill_id = data.get("bill_id") or data.get("record_id") or _synthetic_id(
        file_path, file_index, charge_index
    )
    charges = data.get("charges") or []
    charge = charges[charge_index] if charge_index < len(charges) else {}
    financial = data.get("financial_attributes")
    return ExistingBill(
        bill_id=str(bill_id),
        version=int(data.get("version") or 1),
        document_info=DocumentInfoOut(
            document_type=(data.get("document_info") or {}).get("document_type"),
            reference_no=clean_reference(reference),
            document_date=date_value,
        ),
        shipment_info=shipment,
        transaction_party=TransactionPartyOut(**(data.get("transaction_party") or {})),
        charge=_normalize_existing_charge(charge),
        financial_attributes=(
            FinancialAttributesOut(**financial)
            if isinstance(financial, dict) and financial.get("accounting_entity")
            else None
        ),
        updated_at=data.get("updated_at"),
        source_format="extended",
        derived_keys=derived,
        raw=data,
    )


def _bill_from_minimal(
    data: dict, file_path: Path, file_index: int, settings: Settings
) -> ExistingBill:
    doc_info = data.get("document_info") or {}
    reference = doc_info.get("reference_no")
    date_raw = doc_info.get("document_date")
    date_value = normalize_date(date_raw).value or date_raw
    shipment = ShipmentInfoOut(**(data.get("shipment_info") or {}))
    derived: dict[str, bool] = {}
    if settings.matching.derived_keys_enabled:
        derived = _derive_keys(reference, _stringify(data), shipment)
    bill_id = data.get("bill_id") or data.get("record_id") or _synthetic_id(
        file_path, file_index, 0
    )
    return ExistingBill(
        bill_id=str(bill_id),
        version=int(data.get("version") or 1),
        document_info=DocumentInfoOut(
            document_type=doc_info.get("document_type"),
            reference_no=clean_reference(reference),
            document_date=date_value,
        ),
        shipment_info=shipment,
        transaction_party=TransactionPartyOut(**(data.get("transaction_party") or {})),
        charge=_normalize_existing_charge(data.get("charge_details") or {}),
        financial_attributes=(
            FinancialAttributesOut(**data["financial_attributes"])
            if isinstance(data.get("financial_attributes"), dict)
            and data["financial_attributes"].get("accounting_entity")
            else None
        ),
        updated_at=data.get("updated_at"),
        source_format="minimal",
        derived_keys=derived,
        raw=data,
    )


def _synthetic_id(path: Path, file_index: int, charge_index: int) -> str:
    digest = hashlib.sha256(f"{path}:{file_index}:{charge_index}".encode("utf-8")).hexdigest()
    return f"BILL-{digest[:10].upper()}"


@dataclass
class BillsRepository:
    bills: list[ExistingBill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    l4_relaxed_allowed: bool = True
    l1: dict[tuple[str, str], list[ExistingBill]] = field(default_factory=dict)
    l2: dict[tuple[str, str, str], list[ExistingBill]] = field(default_factory=dict)
    l3: dict[tuple[str, str, str], list[ExistingBill]] = field(default_factory=dict)

    def add(self, bill: ExistingBill) -> None:
        self.bills.append(bill)
        charge_key = _charge_key(bill)
        if bill.document_info.reference_no and charge_key:
            key = (bill.document_info.reference_no, charge_key)
            self.l1.setdefault(key, []).append(bill)
        if bill.shipment_info.bl_no and charge_key and bill.charge.currency:
            key2 = (bill.shipment_info.bl_no, charge_key, bill.charge.currency)
            self.l2.setdefault(key2, []).append(bill)
        if bill.shipment_info.booking_no and charge_key and bill.charge.currency:
            key3 = (bill.shipment_info.booking_no, charge_key, bill.charge.currency)
            self.l3.setdefault(key3, []).append(bill)

    def coverage(self) -> MatchKeyCoverage:
        total = len(self.bills)
        if not total:
            return MatchKeyCoverage()

        def ratio(predicate) -> float:
            return round(sum(1 for bill in self.bills if predicate(bill)) / total, 3)

        return MatchKeyCoverage(
            extended=ratio(lambda b: b.source_format == "extended"),
            minimal=ratio(lambda b: b.source_format == "minimal"),
            L1_ready=ratio(lambda b: bool(b.document_info.reference_no and _charge_key(b))),
            L2_ready=ratio(
                lambda b: bool(b.shipment_info.bl_no and _charge_key(b) and b.charge.currency)
            ),
            L3_ready=ratio(
                lambda b: bool(b.shipment_info.booking_no and _charge_key(b) and b.charge.currency)
            ),
            L4_ready=ratio(
                lambda b: bool(
                    b.transaction_party.issuer
                    and b.transaction_party.recipient
                    and b.document_info.document_date
                    and b.charge.amount is not None
                    and (b.shipment_info.container_no or self.l4_relaxed_allowed)
                )
            ),
        )


def _charge_key(bill: ExistingBill) -> str:
    value = bill.charge.charge_item or bill.charge.original_charge_name
    return normalize_key(value)


def load_bills(paths: list[str | Path] | None, settings: Settings) -> BillsRepository:
    repo = BillsRepository(l4_relaxed_allowed=settings.matching.l4_allow_missing_container)
    if not paths:
        return repo
    files: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(child for child in path.rglob("*.json") if child.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            repo.warnings.append(f"已有账单路径不存在：{item}")

    seen: set[tuple] = set()
    for file_path in files:
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            repo.warnings.append(f"账单 JSON 读取失败 {file_path.name}: {exc}")
            continue
        records: list[dict] = []
        if isinstance(payload, list):
            records = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            if isinstance(payload.get("records"), list):
                records = [item for item in payload["records"] if isinstance(item, dict)]
            else:
                records = [payload]
        for index, data in enumerate(records):
            try:
                if isinstance(data.get("charges"), list) and data["charges"]:
                    for charge_index in range(len(data["charges"])):
                        bill = _bill_from_extended(
                            data, file_path, index, charge_index, settings
                        )
                        key = (
                            bill.bill_id,
                            bill.document_info.reference_no,
                            _charge_key(bill),
                            bill.charge.amount,
                        )
                        if key not in seen:
                            seen.add(key)
                            repo.add(bill)
                elif isinstance(data.get("charge_details"), dict):
                    bill = _bill_from_minimal(data, file_path, index, settings)
                    key = (
                        bill.bill_id,
                        bill.document_info.reference_no,
                        _charge_key(bill),
                        bill.charge.amount,
                    )
                    if key not in seen:
                        seen.add(key)
                        repo.add(bill)
                else:
                    repo.warnings.append(
                        f"无法识别的账单结构（缺 charges / charge_details）：{file_path.name}#{index}"
                    )
            except Exception as exc:
                repo.warnings.append(f"账单解析失败 {file_path.name}#{index}: {exc}")
    return repo
