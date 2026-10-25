"""标准化与 OCR 纠错单测（WP-05 DoD）。"""

from __future__ import annotations

import pytest

from app.agent.nodes.normalize import map_document_type, normalize_document
from app.extraction.schemas import (
    ChargeLine,
    DocumentExtraction,
    ShipmentInfo,
    SourceInfo,
)
from app.governance.dictionaries import load_dicts, normalize_key
from app.governance.ocr_fix import (
    iso6346_check_digit,
    normalize_bl_no,
    normalize_container,
    normalize_date,
)


def _extraction(**overrides) -> DocumentExtraction:
    data = dict(
        document_type="DEBIT NOTE",
        reference_no="DN-2026-001",
        document_date="2026-09-20",
        issuer="Destination Agent",
        recipient="Origin Agent",
        shipment_info=ShipmentInfo(
            bl_no="OOLU4055331281", container_no="TIIU8975975"
        ),
        charges=[
            ChargeLine(
                original_charge_name="H/D Fee",
                quantity=2,
                unit_price=20,
                amount=40,
                currency="US$",
                payment_term="PREPAID",
                snippet="H/D Fee | 2 | 20 | 40 | US$ | PREPAID",
                confidence={"amount": 0.99, "original_charge_name": 0.99},
            )
        ],
        field_confidence={
            "document_type": 0.99,
            "reference_no": 0.99,
            "document_date": 0.99,
            "issuer": 0.99,
            "recipient": 0.99,
            "bl_no": 0.99,
            "container_no": 0.99,
        },
    )
    data.update(overrides)
    return DocumentExtraction(**data)


def _normalize(settings, extraction=None, source_text="H/D Fee 2 20 40 US$ PREPAID"):
    return normalize_document(
        extraction or _extraction(),
        settings,
        source_text=source_text,
        source=SourceInfo(file_name="bill.xlsx", file_type="XLSX", file_sha256="a" * 64),
        uir_id="uir-1",
    )


def test_requirement_fee_mappings():
    dicts = load_dicts()
    cases = {
        "Handling Fee": "Handling Charge",
        "H/D Fee": "Handling Charge",
        "Doc Fee": "Documentation Fee",
        "THC": "Terminal Handling Charge",
        "Trucking Fee": "Trucking Charge",
        "Customs Fee": "Customs Clearance Fee",
        "Storage Fee": "Storage Charge",
        "D/O Fee": "Delivery Order Fee",
        "O/F": "Ocean Freight",
        "BAF": "Bunker Adjustment Factor",
        "CFS Charge": "Container Freight Station Charge",
        "Seal Fee": "Seal Charge",
        "ISPS": "ISPS Charge",
    }
    for original, expected in cases.items():
        assert dicts.fee.match(original).standard == expected


def test_unknown_fee_keeps_original_and_no_standard(settings):
    doc = _normalize(
        settings,
        _extraction(
            charges=[
                ChargeLine(
                    original_charge_name="Special Mystery Fee",
                    amount=10,
                    currency="USD",
                    snippet="Special Mystery Fee 10 USD",
                )
            ]
        ),
        source_text="Special Mystery Fee 10 USD",
    )
    assert doc.charges[0].charge_item is None
    assert doc.charges[0].original_charge_name == "Special Mystery Fee"


def test_currency_aliases_and_ocr_fix():
    dicts = load_dicts()
    assert dicts.currency.normalize("US$") == ("USD", False)
    assert dicts.currency.normalize("$") == ("USD", False)
    assert dicts.currency.normalize("RMB") == ("CNY", False)
    assert dicts.currency.normalize("￥") == ("CNY", False)
    assert dicts.currency.normalize("美元") == ("USD", False)
    assert dicts.currency.normalize("USO") == ("USD", True)
    assert dicts.currency.normalize("比特币") == (None, False)


def test_payment_term_aliases():
    dicts = load_dicts()
    assert dicts.payment.normalize("PREPAID") == "PP"
    assert dicts.payment.normalize("FREIGHT COLLECT") == "CC"
    assert dicts.payment.normalize("到付") == "CC"
    assert dicts.payment.normalize("whatever") is None


def test_document_type_mapping():
    assert map_document_type("DEBIT NOTE") == "Debit Note"
    assert map_document_type("Credit Note") == "Credit Note"
    assert map_document_type("CN") == "Credit Note"
    assert map_document_type("INVOICE") == "Invoice"
    assert map_document_type("Statement") == "Statement"
    assert map_document_type("怪异单据") == "Other"
    assert map_document_type(None) is None


def test_iso6346_known_container_and_repair():
    assert iso6346_check_digit("TIIU8975975") == 5
    assert normalize_container("TIIU8975975").valid
    repaired = normalize_container("TIIU897597S")
    assert repaired.value == "TIIU8975975"
    assert repaired.repaired
    invalid = normalize_container("TIIU8975970")
    assert invalid.valid is False
    multiple = normalize_container("MRSU2571577, TCLU8619685")
    assert multiple.valid
    assert multiple.value == "MRSU2571577"
    assert any("多个箱号" in note for note in multiple.notes)


def test_bl_ocr_correction():
    fix = normalize_bl_no("0OLU4055331281")
    assert fix.value == "OOLU4055331281"
    assert fix.repaired
    assert normalize_bl_no("MSCU1234567").value == "MSCU1234567"


def test_date_formats_and_range():
    assert normalize_date("2026-09-20").value == "2026-09-20"
    assert normalize_date("2026/09/20").value == "2026-09-20"
    assert normalize_date("10-24-2026").value == "2026-10-24"
    ambiguous = normalize_date("20/09/2026")
    assert ambiguous.value == "2026-09-20"
    assert ambiguous.repaired
    assert normalize_date("2026-13-01").value is None
    assert normalize_date("1980-01-01").valid is False


def test_normalize_document_end_to_end(settings):
    doc = _normalize(settings)
    assert doc.document_type == "Debit Note"
    assert doc.reference_no == "DN-2026-001"
    assert doc.document_date == "2026-09-20"
    assert doc.shipment_info.container_no == "TIIU8975975"
    charge = doc.charges[0]
    assert charge.charge_item == "Handling Charge"
    assert charge.standardized_by == "exact"
    assert charge.currency == "USD"
    assert charge.payment_term == "PP"
    assert charge.confidence > 0.9
    assert doc.validation.passed
    assert not any(issue.rule == "V8" and "快照" in issue.message for issue in doc.validation.issues)


def test_unknown_currency_forced_to_review(settings):
    extraction = _extraction(
        charges=[
            ChargeLine(
                original_charge_name="THC",
                amount=40,
                currency="XYZ",
                snippet="THC 40 XYZ",
            )
        ]
    )
    doc = _normalize(settings, extraction, source_text="THC 40 XYZ")
    assert doc.charges[0].currency is None
    assert any(issue.action == "review" for issue in doc.validation.issues)


def test_snippet_not_traceable_lowers_confidence(settings):
    extraction = _extraction(
        charges=[
            ChargeLine(
                original_charge_name="H/D Fee",
                amount=40,
                currency="USD",
                snippet="完全不存在的原文行",
            )
        ]
    )
    doc = _normalize(settings, extraction, source_text="H/D Fee 40 USD")
    assert any("快照" in issue.message for issue in doc.validation.issues)
    assert doc.charges[0].confidence < 0.9


def test_source_confidence_caps_record_confidence(settings):
    doc = normalize_document(
        _extraction(),
        settings,
        source_text="H/D Fee 2 20 40 US$ PREPAID",
        source=SourceInfo(file_name="scan.pdf", file_type="OCR", file_sha256="a" * 64),
        uir_id="uir-ocr",
        source_confidence=0.6,
    )
    assert doc.confidence <= 0.6


def test_fee_key_normalization():
    assert normalize_key("H/D Fee") == normalize_key("h d fee")
    assert normalize_key("  Terminal-Handling_Charge ") == "terminalhandlingcharge"
