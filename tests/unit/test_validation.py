"""校验引擎单测（WP-05 DoD）。"""

from __future__ import annotations

from app.agent.nodes.validate import review_reason_from_validation, validate_document
from app.extraction.schemas import (
    NormalizedCharge,
    NormalizedDocument,
    SourceInfo,
    TotalEntry,
    ValidationIssue,
    ValidationResult,
)


def _charge(name="H/D Fee", amount=40.0, currency="USD", qty=None, price=None) -> NormalizedCharge:
    return NormalizedCharge(
        original_charge_name=name,
        charge_item="Handling Charge",
        amount=amount,
        currency=currency,
        quantity=qty,
        unit_price=price,
        confidence=0.95,
    )


def _doc(charges, totals=None, **overrides) -> NormalizedDocument:
    data = dict(
        uir_id="u1",
        source=SourceInfo(file_name="bill.xlsx", file_type="XLSX", file_sha256="a" * 64),
        document_type="Debit Note",
        reference_no="DN-1",
        document_date="2026-09-20",
        issuer="Destination Agent",
        recipient="Origin Agent",
        charges=charges,
        totals=totals or [],
        confidence=0.95,
        validation=ValidationResult(),
    )
    data.update(overrides)
    return NormalizedDocument(**data)


def test_sum_mismatch_marks_review(settings):
    doc = _doc(
        [_charge(amount=40.0)],
        [TotalEntry(kind="subtotal", amount=50.0, currency="USD")],
    )
    result = validate_document(doc, settings)
    assert result.passed is False
    assert any(issue.rule == "V3" for issue in result.issues)
    assert review_reason_from_validation(result) == "SUM_MISMATCH"


def test_multi_currency_groups_are_checked_separately(settings):
    doc = _doc(
        [_charge(amount=40.0, currency="USD"), _charge(amount=100.0, currency="CNY")],
        [
            TotalEntry(kind="total", amount=40.0, currency="USD"),
            TotalEntry(kind="total", amount=100.0, currency="CNY"),
        ],
    )
    assert validate_document(doc, settings).passed is True


def test_tolerance_accepts_one_cent(settings):
    doc = _doc(
        [_charge(amount=40.0)],
        [TotalEntry(kind="total", amount=40.01, currency="USD")],
    )
    assert validate_document(doc, settings).passed is True
    doc2 = _doc(
        [_charge(amount=40.0)],
        [TotalEntry(kind="total", amount=40.02, currency="USD")],
    )
    assert validate_document(doc2, settings).passed is False


def test_duplicate_charge_deduped(settings):
    doc = _doc([_charge(amount=40.0), _charge(amount=40.0)])
    result = validate_document(doc, settings)
    assert len(doc.charges) == 1
    assert any(issue.rule == "V5" and "去重" in issue.message for issue in result.issues)


def test_summary_row_filtered(settings):
    doc = _doc(
        [_charge(name="Subtotal", amount=140.0), _charge(amount=40.0)],
        [TotalEntry(kind="total", amount=40.0, currency="USD")],
    )
    result = validate_document(doc, settings)
    assert [charge.original_charge_name for charge in doc.charges] == ["H/D Fee"]
    assert any(issue.rule == "V5" and "合计行" in issue.message for issue in result.issues)


def test_quantity_price_mismatch(settings):
    doc = _doc(
        [_charge(amount=40.0, qty=2, price=19.0)],
        [TotalEntry(kind="total", amount=40.0, currency="USD")],
    )
    result = validate_document(doc, settings)
    assert any(issue.rule == "V7" for issue in result.issues)
    assert result.passed is False


def test_single_charge_exceeds_total(settings):
    doc = _doc(
        [_charge(amount=100.0), _charge(amount=40.0)],
        [TotalEntry(kind="total", amount=50.0, currency="USD")],
    )
    result = validate_document(doc, settings)
    assert any(issue.rule == "V9" for issue in result.issues)


def test_credit_note_negative_totals_match(settings):
    doc = _doc(
        [_charge(name="Doc Fee", amount=-35.0)],
        [TotalEntry(kind="total", amount=-35.0, currency="USD")],
        document_type="Credit Note",
    )
    assert validate_document(doc, settings).passed is True


def test_missing_amount_review(settings):
    doc = _doc([_charge(amount=None)])
    result = validate_document(doc, settings)
    assert result.passed is False
    assert review_reason_from_validation(result) == "OTHER"


def test_no_charges_is_not_a_validation_failure(settings):
    doc = _doc([])
    result = validate_document(doc, settings)
    assert result.passed is True
    assert any(issue.rule == "NO_CHARGES" for issue in result.issues)


def test_failure_lowers_document_confidence(settings):
    doc = _doc(
        [_charge(amount=40.0)],
        [TotalEntry(kind="subtotal", amount=999.0, currency="USD")],
    )
    validate_document(doc, settings)
    assert doc.confidence < settings.thresholds.low_confidence_below
