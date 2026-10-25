"""标准化与确定性清洗（FR-07/08/14，D4/D11 / WP-05）。"""

from __future__ import annotations

import re

from app.config.settings import Settings
from app.extraction.schemas import (
    ChargeLine,
    DocumentExtraction,
    NormalizedCharge,
    NormalizedDocument,
    ShipmentInfo,
    SourceInfo,
    TotalEntry,
    ValidationIssue,
    ValidationResult,
    coerce_number,
)
from app.governance.dictionaries import load_dicts
from app.governance.ocr_fix import (
    clean_reference,
    normalize_bl_no,
    normalize_booking_no,
    normalize_container,
    normalize_date,
)

_DOC_TYPE_CN = {
    "借记通知单": "Debit Note",
    "贷记通知单": "Credit Note",
    "发票": "Invoice",
    "对账单": "Statement",
}


def map_document_type(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).upper().strip()
    if "CREDIT" in text or text in {"CN", "C/N"} or "贷记" in text:
        return "Credit Note"
    if "DEBIT" in text or text in {"DN", "D/N"} or "借记" in text:
        return "Debit Note"
    if "INVOICE" in text or text in {"INV"} or "发票" in text:
        return "Invoice"
    if "STATEMENT" in text or "对账" in text:
        return "Statement"
    for cn, mapped in _DOC_TYPE_CN.items():
        if cn in text:
            return mapped
    return "Other"


def _confidence_of_line(charge: ChargeLine, penalties: float) -> float:
    mapping = {
        "original_charge_name": charge.original_charge_name,
        "quantity": charge.quantity,
        "unit_price": charge.unit_price,
        "amount": charge.amount,
        "currency": charge.currency,
        "payment_term": charge.payment_term,
    }
    values = [
        charge.confidence[field]
        for field, value in mapping.items()
        if value is not None and field in charge.confidence
    ]
    base = min(values) if values else 0.8
    return max(0.0, min(1.0, base - penalties))


def _compact(value: str) -> str:
    def canonical(match: re.Match) -> str:
        number = coerce_number(match.group(0))
        return f"{number:.2f}" if number is not None else match.group(0)

    text = re.sub(r"[-+]?\(?\d[\d,]*(?:\.\d+)?\)?", canonical, value or "")
    return re.sub(r"[\s|]+", "", text).lower()


def _numbers(text: str) -> set[float]:
    result: set[float] = set()
    for match in re.finditer(r"[-+]?\(?\d[\d,]*(?:\.\d+)?\)?", text or ""):
        value = coerce_number(match.group(0))
        if value is not None:
            result.add(round(value, 2))
    return result


def _snippet_status(
    snippet: str | None, source_text: str, charge: ChargeLine | None = None
) -> str:
    """literal=逐字（含数字格式归一）命中；values=名/金额可溯源；missing=无法溯源。"""
    if not snippet or not snippet.strip():
        return "missing"
    if _compact(snippet) in _compact(source_text):
        return "literal"
    if charge is None:
        return "missing"
    name_found = bool(
        charge.original_charge_name
        and _compact(charge.original_charge_name) in _compact(source_text)
    )
    amount_found = bool(
        charge.amount is not None and round(charge.amount, 2) in _numbers(source_text)
    )
    if name_found and (charge.amount is None or amount_found):
        return "values"
    if amount_found and not charge.original_charge_name:
        return "values"
    return "missing"


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text or None


def normalize_document(
    extraction: DocumentExtraction,
    settings: Settings,
    source_text: str = "",
    source: SourceInfo | None = None,
    uir_id: str = "",
    source_confidence: float = 1.0,
) -> NormalizedDocument:
    dicts = load_dicts()
    issues: list[ValidationIssue] = []
    repairs = 0

    document_type = map_document_type(extraction.document_type)
    if extraction.document_type and not document_type:
        issues.append(
            ValidationIssue(
                rule="V0",
                message=f"无法识别单据类型：{extraction.document_type}",
                action="none",
                field="document_type",
            )
        )
    reference_no = clean_reference(extraction.reference_no)
    date_fix = normalize_date(extraction.document_date)
    if date_fix.repaired:
        repairs += 1
    for note in date_fix.notes:
        issues.append(
            ValidationIssue(
                rule="V6",
                message=note,
                action="none" if date_fix.valid else "review",
                field="document_date",
                severity="info" if date_fix.valid and "缺失" in note else "warning",
            )
        )
    if date_fix.value is None and extraction.document_date:
        issues.append(
            ValidationIssue(
                rule="V6",
                message=f"日期无法归一：{extraction.document_date}",
                action="review",
                field="document_date",
                severity="error",
            )
        )

    bl_fix = normalize_bl_no(extraction.shipment_info.bl_no)
    booking_fix = normalize_booking_no(extraction.shipment_info.booking_no)
    container_fix = normalize_container(extraction.shipment_info.container_no)
    for field_name, fix in (
        ("bl_no", bl_fix),
        ("booking_no", booking_fix),
        ("container_no", container_fix),
    ):
        if fix.repaired:
            repairs += 1
        for note in fix.notes:
            if "缺失" in note:
                issues.append(
                    ValidationIssue(
                        rule="V4",
                        message=note,
                        action="none",
                        field=field_name,
                        severity="info",
                    )
                )
                continue
            issues.append(
                ValidationIssue(
                    rule="V4",
                    message=note,
                    action="none" if fix.valid else "review",
                    field=field_name,
                    severity="warning" if fix.valid else "error",
                )
            )

    charges: list[NormalizedCharge] = []
    for line in extraction.charges:
        line_issues: list[ValidationIssue] = []
        penalties = 0.0
        original_name = _clean_name(line.original_charge_name)
        fee_match = dicts.fee.match(original_name)
        if original_name and fee_match.standard is None:
            line_issues.append(
                ValidationIssue(
                    rule="V0",
                    message=f"费用名未收录，保留原文：{original_name}",
                    action="none",
                    field="original_charge_name",
                    severity="info",
                )
            )
        elif not original_name:
            line_issues.append(
                ValidationIssue(
                    rule="V1",
                    message="费用名称缺失",
                    action="review",
                    field="original_charge_name",
                    severity="error",
                )
            )
            penalties += 0.2
        if fee_match.method == "fuzzy":
            penalties += 0.05

        currency_raw = (line.currency or "").strip() or None
        currency, currency_repaired = dicts.currency.normalize(currency_raw)
        if currency_raw and not currency:
            line_issues.append(
                ValidationIssue(
                    rule="V2",
                    message=f"币种无法归一到 ISO 4217，保留原文：{currency_raw}",
                    action="review",
                    field="currency",
                    severity="error",
                )
            )
            penalties += 0.25
        elif currency_repaired:
            line_issues.append(
                ValidationIssue(
                    rule="V4",
                    message=f"币种 OCR 纠错：{currency_raw} → {currency}",
                    action="none",
                    field="currency",
                    severity="info",
                )
            )
            penalties += 0.05
            repairs += 1
        elif not currency_raw:
            line_issues.append(
                ValidationIssue(
                    rule="V2",
                    message="币种缺失",
                    action="none",
                    field="currency",
                    severity="warning",
                )
            )
            penalties += 0.15

        payment_term = dicts.payment.normalize(line.payment_term)
        if line.payment_term and payment_term is None:
            line_issues.append(
                ValidationIssue(
                    rule="V8",
                    message=f"付款方式无法识别，已置空：{line.payment_term}",
                    action="none",
                    field="payment_term",
                    severity="info",
                )
            )

        amount = line.amount
        if amount is None and line.quantity is not None and line.unit_price is not None:
            amount = round(line.quantity * line.unit_price, 2)
            line_issues.append(
                ValidationIssue(
                    rule="V7",
                    message=(
                        f"金额缺失，按数量×单价推导：{line.quantity} × {line.unit_price} = {amount}"
                    ),
                    action="none",
                    field="amount",
                    severity="warning",
                )
            )
            repairs += 1
            penalties += 0.05
        elif amount is None:
            line_issues.append(
                ValidationIssue(
                    rule="V1",
                    message="金额缺失或无法解析",
                    action="review",
                    field="amount",
                    severity="error",
                )
            )
            penalties += 0.3

        traceable = _snippet_status(line.snippet, source_text, line)
        if traceable == "missing":
            line_issues.append(
                ValidationIssue(
                    rule="V8",
                    message="费用行快照无法在原文中逐字溯源，已降低置信度",
                    action="none",
                    field="snippet",
                    severity="warning",
                )
            )
            penalties += 0.2
        elif traceable == "values":
            line_issues.append(
                ValidationIssue(
                    rule="V8",
                    message="费用行快照与原文未逐字一致，但费用名与金额可在原文溯源",
                    action="none",
                    field="snippet",
                    severity="info",
                )
            )
            penalties += 0.1
        elif not line.snippet:
            penalties += 0.05

        confidence = _confidence_of_line(line, min(penalties, 0.6))
        charges.append(
            NormalizedCharge(
                source_line_id=line.line_id,
                original_charge_name=original_name,
                charge_item=fee_match.standard,
                standardized_by=fee_match.method if fee_match.standard else None,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=amount,
                currency=currency,
                payment_term=payment_term,
                snippet=line.snippet,
                locator=line.locator,
                field_confidence=line.confidence,
                confidence=confidence,
            )
        )
        issues.extend(line_issues)

    totals: list[TotalEntry] = []
    for total in extraction.totals:
        currency, repaired = dicts.currency.normalize(total.currency)
        if total.currency and not currency:
            issues.append(
                ValidationIssue(
                    rule="V2",
                    message=f"合计行币种无法归一：{total.currency}",
                    action="review",
                    field="totals",
                    severity="error",
                )
            )
        if repaired:
            repairs += 1
        totals.append(TotalEntry(kind=total.kind, amount=total.amount, currency=currency or total.currency))

    field_confidence = dict(extraction.field_confidence)
    confidence_inputs: list[float] = []
    for field, value in (
        ("document_type", document_type),
        ("reference_no", reference_no),
        ("document_date", date_fix.value),
        ("issuer", extraction.issuer),
        ("recipient", extraction.recipient),
        ("bl_no", bl_fix.value),
        ("booking_no", booking_fix.value),
        ("container_no", container_fix.value),
    ):
        if value:
            confidence_inputs.append(field_confidence.get(field, 0.85))
    confidence_inputs.extend(charge.confidence for charge in charges)
    confidence = min(confidence_inputs) if confidence_inputs else 0.0
    confidence = max(0.0, confidence - min(0.2, 0.02 * repairs))
    confidence = min(confidence, max(0.0, min(1.0, source_confidence)))

    validation = ValidationResult(passed=True, issues=issues)
    # 由 validate_document 复核 passed
    return NormalizedDocument(
        uir_id=uir_id or extraction.source_chunk_id or "",
        source=source or SourceInfo(file_name="unknown"),
        document_type=document_type,
        reference_no=reference_no,
        document_date=date_fix.value,
        shipment_info=ShipmentInfo(
            bl_no=bl_fix.value,
            booking_no=booking_fix.value,
            container_no=container_fix.value,
            vessel_voyage=extraction.shipment_info.vessel_voyage,
            pol=extraction.shipment_info.pol,
            pod=extraction.shipment_info.pod,
        ),
        issuer=extraction.issuer,
        recipient=extraction.recipient,
        direction_statement=extraction.direction_statement,
        charges=charges,
        totals=totals,
        field_confidence=field_confidence,
        confidence=confidence,
        validation=validation,
        raw=extraction,
    )
