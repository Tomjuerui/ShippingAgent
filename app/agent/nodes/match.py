"""四级匹配与增量更新（FR-09/11/12，D4/D5/D10 / WP-07）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.agent.nodes.normalize import map_document_type
from app.config.settings import Settings
from app.extraction.schemas import (
    ChangeInfoOut,
    ChangedField,
    ChargeDetailsOut,
    DocumentInfoOut,
    InheritedField,
    MatchLevel,
    MatchResult,
    NormalizedCharge,
    NormalizedDocument,
    Operation,
    ShipmentInfoOut,
    TransactionPartyOut,
)
from app.governance.dictionaries import load_dicts, normalize_key
from app.governance.ocr_fix import clean_reference, normalize_date
from app.storage.bills_repo import BillsRepository, ExistingBill


def _charge_key(charge_item: str | None, original_name: str | None) -> str:
    return normalize_key(charge_item or original_name)


def _candidates_with_contains(
    doc_value: str | None,
    bill_value: str | None,
) -> bool:
    a = normalize_key(clean_reference(doc_value))
    b = normalize_key(clean_reference(bill_value))
    return bool(a and b and (a in b or b in a))


def _result(
    level: MatchLevel,
    bill: ExistingBill | None,
    evidence: str,
    candidates: list[ExistingBill] | None = None,
    derived: list[str] | None = None,
) -> MatchResult:
    candidates = candidates or ([] if bill is None else [bill])
    return MatchResult(
        level=level,
        bill=bill,
        candidates=candidates,
        evidence=evidence,
        ambiguous=len(candidates) > 1,
        derived_keys_used=derived or [],
    )


def _derived_used(bill: ExistingBill, fields: list[str]) -> list[str]:
    return [field for field in fields if bill.derived_keys.get(field)]


def _match_l1(doc: NormalizedDocument, charge: NormalizedCharge, repo: BillsRepository) -> MatchResult | None:
    if not doc.reference_no:
        return None
    key_charge = _charge_key(charge.charge_item, charge.original_charge_name)
    if not key_charge:
        return None
    exact = repo.l1.get((clean_reference(doc.reference_no) or "", key_charge), [])
    if exact:
        evidence = f"L1 reference_no={doc.reference_no} + charge_item={charge.charge_item or charge.original_charge_name}"
        if len(exact) == 1:
            return _result(MatchLevel.L1, exact[0], evidence, exact, _derived_used(exact[0], ["reference_no"]))
        return _result(MatchLevel.L1, None, evidence + "（多命中）", exact)
    fuzzy = [
        bill
        for bill in repo.bills
        if _candidates_with_contains(doc.reference_no, bill.document_info.reference_no)
        and _charge_key(bill.charge.charge_item, bill.charge.original_charge_name) == key_charge
    ]
    if fuzzy:
        evidence = f"L1 模糊包含 reference_no={doc.reference_no} + charge_item={charge.charge_item}"
        if len(fuzzy) == 1:
            return _result(MatchLevel.L1, fuzzy[0], evidence, fuzzy)
        return _result(MatchLevel.L1, None, evidence + "（多命中）", fuzzy)
    return None


def _match_l2(doc: NormalizedDocument, charge: NormalizedCharge, repo: BillsRepository) -> MatchResult | None:
    bl_no = doc.shipment_info.bl_no
    if not bl_no or not charge.currency:
        return None
    key_charge = _charge_key(charge.charge_item, charge.original_charge_name)
    if not key_charge:
        return None
    exact = repo.l2.get((clean_reference(bl_no) or "", key_charge, charge.currency), [])
    if exact:
        evidence = f"L2 bl_no={bl_no} + charge_item={charge.charge_item or charge.original_charge_name} + currency={charge.currency}"
        if len(exact) == 1:
            return _result(MatchLevel.L2, exact[0], evidence, exact, _derived_used(exact[0], ["bl_no"]))
        return _result(MatchLevel.L2, None, evidence + "（多命中）", exact)
    fuzzy = [
        bill
        for bill in repo.bills
        if bill.charge.currency == charge.currency
        and _charge_key(bill.charge.charge_item, bill.charge.original_charge_name) == key_charge
        and (
            _candidates_with_contains(bl_no, bill.shipment_info.bl_no)
            or _candidates_with_contains(bl_no, bill.document_info.reference_no)
        )
    ]
    if fuzzy:
        evidence = f"L2 模糊包含 bl_no={bl_no} + charge_item={charge.charge_item} + currency={charge.currency}"
        if len(fuzzy) == 1:
            return _result(MatchLevel.L2, fuzzy[0], evidence, fuzzy, _derived_used(fuzzy[0], ["bl_no"]))
        return _result(MatchLevel.L2, None, evidence + "（多命中）", fuzzy)
    return None


def _match_l3(doc: NormalizedDocument, charge: NormalizedCharge, repo: BillsRepository) -> MatchResult | None:
    booking_no = doc.shipment_info.booking_no
    if not booking_no or not charge.currency:
        return None
    key_charge = _charge_key(charge.charge_item, charge.original_charge_name)
    if not key_charge:
        return None
    exact = repo.l3.get((clean_reference(booking_no) or "", key_charge, charge.currency), [])
    if exact:
        evidence = f"L3 booking_no={booking_no} + charge_item={charge.charge_item or charge.original_charge_name} + currency={charge.currency}"
        if len(exact) == 1:
            return _result(MatchLevel.L3, exact[0], evidence, exact, _derived_used(exact[0], ["booking_no"]))
        return _result(MatchLevel.L3, None, evidence + "（多命中）", exact)
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    fix = normalize_date(value)
    if not fix.value:
        return None
    try:
        return date.fromisoformat(fix.value)
    except ValueError:
        return None


def _party_equal(a: str | None, b: str | None) -> bool:
    normalizer = load_dicts().entity
    return bool(a and b and normalizer.normalize(a) == normalizer.normalize(b))


def _match_l4(
    doc: NormalizedDocument, charge: NormalizedCharge, repo: BillsRepository, settings: Settings
) -> MatchResult | None:
    if not settings.matching.l4_enabled:
        return None
    date_value = _parse_date(doc.document_date)
    if date_value is None or charge.amount is None:
        return None
    if not (doc.issuer and doc.recipient):
        return None
    doc_container = clean_reference(doc.shipment_info.container_no)
    candidates: list[ExistingBill] = []
    relaxed = False
    for bill in repo.bills:
        bill_date = _parse_date(bill.document_info.document_date)
        if bill_date is None:
            continue
        if abs((bill_date - date_value).days) > settings.matching.date_window_days:
            continue
        if bill.charge.amount is None:
            continue
        if abs(bill.charge.amount - charge.amount) > settings.matching.amount_tolerance:
            continue
        if charge.currency and bill.charge.currency and charge.currency != bill.charge.currency:
            continue
        if not (_party_equal(doc.issuer, bill.transaction_party.issuer) and _party_equal(doc.recipient, bill.transaction_party.recipient)):
            continue
        bill_container = clean_reference(bill.shipment_info.container_no)
        if doc_container and bill_container:
            if doc_container != bill_container:
                continue
        elif not settings.matching.l4_allow_missing_container:
            continue
        else:
            relaxed = True
        candidates.append(bill)
    if not candidates:
        return None
    evidence = (
        f"L4 container_no={doc_container or '(缺失，已放宽)'} + issuer/recipient + "
        f"date±{settings.matching.date_window_days}d + amount±{settings.matching.amount_tolerance}"
    )
    if relaxed:
        evidence += "（箱号缺失，按双方+日期+金额子集匹配）"
    if len(candidates) == 1:
        return _result(
            MatchLevel.L4,
            candidates[0],
            evidence,
            candidates,
            _derived_used(candidates[0], ["container_no"]),
        )
    return _result(MatchLevel.L4, None, evidence + "（多命中）", candidates)


def match_charge(
    doc: NormalizedDocument,
    charge: NormalizedCharge,
    repo: BillsRepository,
    settings: Settings,
) -> MatchResult:
    for matcher in (_match_l1, _match_l2, _match_l3):
        result = matcher(doc, charge, repo)
        if result is not None:
            return result
    result = _match_l4(doc, charge, repo, settings)
    if result is not None:
        return result
    return MatchResult(level=MatchLevel.NONE)


# --------------------------------------------------------------------------- #
# 字段级 Diff（D5：null 不覆盖；不采信 LLM 自评）
# --------------------------------------------------------------------------- #
@dataclass
class MergedValues:
    document_info: DocumentInfoOut
    shipment_info: ShipmentInfoOut
    transaction_party: TransactionPartyOut
    charge_details: ChargeDetailsOut
    change_info: ChangeInfoOut | None = None
    conflict: bool = False
    conflict_notes: list[str] = field(default_factory=list)


def _norm_text(value) -> str:
    return str(value).strip().casefold() if value not in (None, "") else ""


def _norm_doc_type(value: str | None) -> str:
    return (map_document_type(value) or _norm_text(value)).casefold()


def _equal(field: str, new, old) -> bool:
    if isinstance(new, (int, float)) or isinstance(old, (int, float)):
        try:
            return abs(float(new) - float(old)) <= 0.01
        except (TypeError, ValueError):
            return False
    if field.endswith(("reference_no", "bl_no", "booking_no", "container_no")):
        return (clean_reference(new) or "") == (clean_reference(old) or "")
    if field.endswith("document_type"):
        return _norm_doc_type(new) == _norm_doc_type(old)
    if field.endswith("document_date"):
        return (normalize_date(new).value or _norm_text(new)) == (normalize_date(old).value or _norm_text(old))
    if field.endswith("currency"):
        return (str(new).upper() if new else "") == (str(old).upper() if old else "")
    return _norm_text(new) == _norm_text(old)


def _field_pairs(doc: NormalizedDocument, charge: NormalizedCharge, bill: ExistingBill) -> list[tuple[str, object, object]]:
    return [
        ("document_info.document_type", map_document_type(doc.document_type), bill.document_info.document_type),
        ("document_info.reference_no", doc.reference_no, bill.document_info.reference_no),
        ("document_info.document_date", doc.document_date, bill.document_info.document_date),
        ("shipment_info.bl_no", doc.shipment_info.bl_no, bill.shipment_info.bl_no),
        ("shipment_info.booking_no", doc.shipment_info.booking_no, bill.shipment_info.booking_no),
        ("shipment_info.container_no", doc.shipment_info.container_no, bill.shipment_info.container_no),
        ("shipment_info.vessel_voyage", doc.shipment_info.vessel_voyage, bill.shipment_info.vessel_voyage),
        ("shipment_info.pol", doc.shipment_info.pol, bill.shipment_info.pol),
        ("shipment_info.pod", doc.shipment_info.pod, bill.shipment_info.pod),
        ("transaction_party.issuer", doc.issuer, bill.transaction_party.issuer),
        ("transaction_party.recipient", doc.recipient, bill.transaction_party.recipient),
        ("charge_details.original_charge_name", charge.original_charge_name, bill.charge.original_charge_name),
        ("charge_details.charge_item", charge.charge_item, bill.charge.charge_item),
        ("charge_details.quantity", charge.quantity, bill.charge.quantity),
        ("charge_details.unit_price", charge.unit_price, bill.charge.unit_price),
        ("charge_details.amount", charge.amount, bill.charge.amount),
        ("charge_details.currency", charge.currency, bill.charge.currency),
        ("charge_details.payment_term", charge.payment_term, bill.charge.payment_term),
    ]


def _detect_conflicts(
    doc: NormalizedDocument, charge: NormalizedCharge, bill: ExistingBill
) -> list[str]:
    notes: list[str] = []
    same_reference = (
        doc.reference_no
        and bill.document_info.reference_no
        and clean_reference(doc.reference_no) == clean_reference(bill.document_info.reference_no)
    )
    if not same_reference:
        return notes
    if (
        doc.document_type
        and bill.document_info.document_type
        and _norm_doc_type(doc.document_type) != _norm_doc_type(bill.document_info.document_type)
    ):
        notes.append(
            f"相同单据编号 {doc.reference_no} 的单据类型冲突："
            f"{doc.document_type} vs {bill.document_info.document_type}"
        )
    issuer_differs = (
        doc.issuer
        and bill.transaction_party.issuer
        and not _party_equal(doc.issuer, bill.transaction_party.issuer)
    )
    recipient_differs = (
        doc.recipient
        and bill.transaction_party.recipient
        and not _party_equal(doc.recipient, bill.transaction_party.recipient)
    )
    if issuer_differs and recipient_differs:
        notes.append(
            f"相同单据编号 {doc.reference_no} 的签发方/接收方同时冲突："
            f"{doc.issuer}/{doc.recipient} vs {bill.transaction_party.issuer}/{bill.transaction_party.recipient}"
        )
    return notes


def merge_update(
    doc: NormalizedDocument,
    charge: NormalizedCharge,
    bill: ExistingBill,
) -> MergedValues:
    changed: list[ChangedField] = []
    inherited: list[InheritedField] = []
    merged: dict[str, object] = {}
    for field_name, new_value, old_value in _field_pairs(doc, charge, bill):
        if new_value in (None, ""):
            merged[field_name] = old_value
            if old_value not in (None, ""):
                inherited.append(InheritedField(field=field_name, value=old_value))
        elif old_value in (None, ""):
            merged[field_name] = new_value
            changed.append(ChangedField(field=field_name, old_value=old_value, new_value=new_value))
        elif not _equal(field_name, new_value, old_value):
            merged[field_name] = new_value
            changed.append(ChangedField(field=field_name, old_value=old_value, new_value=new_value))
        else:
            merged[field_name] = old_value

    conflicts = _detect_conflicts(doc, charge, bill)
    values = MergedValues(
        document_info=DocumentInfoOut(
            document_type=merged["document_info.document_type"],
            reference_no=merged["document_info.reference_no"],
            document_date=merged["document_info.document_date"],
        ),
        shipment_info=ShipmentInfoOut(
            bl_no=merged["shipment_info.bl_no"],
            booking_no=merged["shipment_info.booking_no"],
            container_no=merged["shipment_info.container_no"],
            vessel_voyage=merged["shipment_info.vessel_voyage"],
            pol=merged["shipment_info.pol"],
            pod=merged["shipment_info.pod"],
        ),
        transaction_party=TransactionPartyOut(
            issuer=merged["transaction_party.issuer"],
            recipient=merged["transaction_party.recipient"],
        ),
        charge_details=ChargeDetailsOut(
            original_charge_name=merged["charge_details.original_charge_name"],
            charge_item=merged["charge_details.charge_item"],
            quantity=merged["charge_details.quantity"],
            unit_price=merged["charge_details.unit_price"],
            amount=merged["charge_details.amount"],
            currency=merged["charge_details.currency"],
            payment_term=merged["charge_details.payment_term"],
        ),
        change_info=ChangeInfoOut(changed_fields=changed, inherited_fields=inherited),
        conflict=bool(conflicts),
        conflict_notes=conflicts,
    )
    return values


def create_values(doc: NormalizedDocument, charge: NormalizedCharge) -> MergedValues:
    return MergedValues(
        document_info=DocumentInfoOut(
            document_type=doc.document_type,
            reference_no=doc.reference_no,
            document_date=doc.document_date,
        ),
        shipment_info=ShipmentInfoOut(**doc.shipment_info.model_dump()),
        transaction_party=TransactionPartyOut(issuer=doc.issuer, recipient=doc.recipient),
        charge_details=ChargeDetailsOut(
            original_charge_name=charge.original_charge_name,
            charge_item=charge.charge_item,
            quantity=charge.quantity,
            unit_price=charge.unit_price,
            amount=charge.amount,
            currency=charge.currency,
            payment_term=charge.payment_term,
        ),
        change_info=None,
    )


def decide_operation(
    match: MatchResult,
    doc: NormalizedDocument,
    charge: NormalizedCharge,
    settings: Settings,
) -> tuple[Operation, MergedValues, str | None, list[str]]:
    """返回 (operation, merged, review_reason, reasoning_notes)。"""
    notes: list[str] = []
    if match.level == MatchLevel.NONE or match.bill is None:
        if match.ambiguous:
            return Operation.PENDING_REVIEW, create_values(doc, charge), "MATCH_AMBIGUOUS", [
                f"匹配到 {len(match.candidates)} 条候选账单，无法唯一确定"
            ]
        return Operation.CREATE, create_values(doc, charge), None, ["L1–L4 均未匹配到已有账单，判定为新增"]

    bill = match.bill
    merged = merge_update(doc, charge, bill)
    if merged.conflict:
        return (
            Operation.PENDING_REVIEW,
            merged,
            "DATA_CONFLICT",
            merged.conflict_notes,
        )
    if settings.matching.l4_enabled and settings.matching.l4_to_review and match.level == MatchLevel.L4:
        return (
            Operation.PENDING_REVIEW,
            merged,
            "OTHER",
            ["L4 弱键命中（箱号+双方+日期+金额），按配置送人工复核"],
        )
    if merged.change_info and merged.change_info.changed_fields:
        return (
            Operation.UPDATE,
            merged,
            None,
            [f"匹配到已有账单 {bill.bill_id}（{match.level.value}），存在字段差异，判定为更新"],
        )
    return (
        Operation.NOCHANGE,
        merged,
        None,
        [f"匹配到已有账单 {bill.bill_id}（{match.level.value}），所有字段无差异"],
    )
