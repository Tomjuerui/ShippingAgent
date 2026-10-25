"""四级匹配、派生键、Diff 继承与冲突单测（WP-07 DoD）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.nodes.match import decide_operation, match_charge
from app.extraction.schemas import (
    NormalizedCharge,
    NormalizedDocument,
    Operation,
    ShipmentInfo,
    SourceInfo,
    ValidationResult,
)
from app.storage.bills_repo import load_bills


def _charge(name="H/D Fee", standard="Handling Charge", amount=40.0, currency="USD", qty=None) -> NormalizedCharge:
    return NormalizedCharge(
        original_charge_name=name,
        charge_item=standard,
        amount=amount,
        currency=currency,
        quantity=qty,
        confidence=0.95,
    )


def _doc(
    ref="DN-2026-001",
    bl=None,
    booking=None,
    container=None,
    issuer="Destination Agent",
    recipient="Origin Agent",
    date="2026-09-20",
    doc_type="Debit Note",
    charge=None,
) -> NormalizedDocument:
    return NormalizedDocument(
        uir_id="u1",
        source=SourceInfo(file_name="bill.xlsx", file_type="XLSX", file_sha256="d" * 64),
        document_type=doc_type,
        reference_no=ref,
        document_date=date,
        shipment_info=ShipmentInfo(bl_no=bl, booking_no=booking, container_no=container),
        issuer=issuer,
        recipient=recipient,
        charges=[charge or _charge()],
        confidence=0.95,
        validation=ValidationResult(),
    )


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _extended_bill(bill_id="BILL-1", ref="DN-2026-001", amount=35.0, standard="Handling Charge", qty=2):
    return {
        "bill_id": bill_id,
        "version": 1,
        "document_info": {
            "document_type": "Debit Note",
            "reference_no": ref,
            "document_date": "2026-09-20",
        },
        "shipment_info": {
            "bl_no": "OOLU4055331281",
            "booking_no": "BK-2026-889",
            "container_no": "TIIU8975975",
        },
        "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
        "charges": [
            {
                "charge_item": standard,
                "original_charge_name": "H/D Fee",
                "quantity": qty,
                "unit_price": 20.0,
                "amount": amount,
                "currency": "USD",
                "payment_term": "PP",
            }
        ],
        "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
    }


def _minimal_bill(bill_id="BILL-MIN", ref="OOLU4055331281", amount=40.0):
    return {
        "bill_id": bill_id,
        "document_info": {
            "document_type": "Debit Note",
            "reference_no": ref,
            "document_date": "2026-09-20",
        },
        "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
        "charge_details": {
            "charge_item": "Handling Charge",
            "amount": amount,
            "currency": "USD",
            "payment_term": "PP",
        },
        "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
        "audit_trail": {"ai_reasoning": ["通过单据编号和费用名称匹配到已有账单"]},
    }


def test_l1_update_amount(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "bill.json", _extended_bill())], settings)
    doc = _doc()
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L1"
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.UPDATE
    fields = {change.field for change in merged.change_info.changed_fields}
    assert "charge_details.amount" in fields
    amount_change = next(
        change for change in merged.change_info.changed_fields if change.field == "charge_details.amount"
    )
    assert amount_change.old_value == 35.0
    assert amount_change.new_value == 40.0


def test_l1_nochange(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "bill.json", _extended_bill(amount=40.0))], settings)
    doc = _doc()
    match = match_charge(doc, doc.charges[0], repo, settings)
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.NOCHANGE
    assert merged.change_info.changed_fields == []


def test_l1_ambiguous(settings, tmp_path):
    repo = load_bills(
        [
            _write(tmp_path / "b1.json", _extended_bill("BILL-1")),
            _write(tmp_path / "b2.json", _extended_bill("BILL-2")),
        ],
        settings,
    )
    doc = _doc()
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.ambiguous
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.PENDING_REVIEW
    assert review == "MATCH_AMBIGUOUS"


def test_fuzzy_reference_contains(settings, tmp_path):
    repo = load_bills(
        [_write(tmp_path / "bill.json", _extended_bill(ref="DN-2026-001-EXTRA"))], settings
    )
    doc = _doc(ref="DN-2026-001")
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L1"
    assert "模糊" in match.evidence


def test_minimal_format_derives_bl_and_matches_l2(settings, tmp_path):
    path = _write(tmp_path / "min.json", _minimal_bill())
    repo = load_bills([path], settings)
    bill = repo.bills[0]
    assert bill.source_format == "minimal"
    assert bill.shipment_info.bl_no == "OOLU4055331281"
    assert bill.derived_keys.get("bl_no") is True

    doc = _doc(ref=None, bl="OOLU4055331281")
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L2"
    assert "bl_no" in match.derived_keys_used


def test_minimal_container_reference_matches_l4_and_reviews(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "min.json", _minimal_bill(ref="TIIU8975975"))], settings)
    bill = repo.bills[0]
    assert bill.shipment_info.container_no == "TIIU8975975"
    assert bill.derived_keys.get("container_no") is True
    doc = _doc(ref=None, container="TIIU8975975")
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L4"
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.PENDING_REVIEW
    assert review == "OTHER"


def test_minimal_booking_reference_matches_l3(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "min.json", _minimal_bill(ref="BK-2026-889"))], settings)
    bill = repo.bills[0]
    assert bill.shipment_info.booking_no == "BK-2026-889"
    doc = _doc(ref=None, booking="BK-2026-889")
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L3"


def test_minimal_remarks_scan_derived_keys(settings, tmp_path):
    data = _minimal_bill(ref="REF-XYZ")
    data["remarks"] = "Container TIIU8975975 / MBL OOLU4055331281"
    repo = load_bills([_write(tmp_path / "min.json", data)], settings)
    bill = repo.bills[0]
    assert bill.shipment_info.container_no == "TIIU8975975"
    assert bill.shipment_info.bl_no == "OOLU4055331281"
    assert bill.derived_keys.get("container_no") and bill.derived_keys.get("bl_no")


def test_thc_alias_matches_standard_name(settings, tmp_path):
    repo = load_bills(
        [
            _write(
                tmp_path / "bill.json",
                _extended_bill(standard="Terminal Handling Charge", amount=100.0),
            )
        ],
        settings,
    )
    doc = _doc()
    doc.charges = [_charge(name="THC", standard="Terminal Handling Charge", amount=100.0)]
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value == "L1"


def test_null_does_not_overwrite(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "bill.json", _extended_bill(qty=2))], settings)
    doc = _doc()
    doc.charges[0].quantity = None
    match = match_charge(doc, doc.charges[0], repo, settings)
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.UPDATE  # 金额 35 → 40 仍是变更
    assert merged.charge_details.quantity == 2
    changed_fields = {change.field for change in merged.change_info.changed_fields}
    assert "charge_details.quantity" not in changed_fields
    inherited = {item.field for item in merged.change_info.inherited_fields}
    assert "charge_details.quantity" in inherited


def test_data_conflict_same_reference(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "bill.json", _extended_bill())], settings)
    doc = _doc(issuer="Fraud Co.", recipient="Other Co.")
    match = match_charge(doc, doc.charges[0], repo, settings)
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.PENDING_REVIEW
    assert review == "DATA_CONFLICT"
    assert merged.conflict


def test_no_match_creates(settings, tmp_path):
    repo = load_bills([], settings)
    doc = _doc(ref="DN-UNKNOWN")
    match = match_charge(doc, doc.charges[0], repo, settings)
    operation, merged, review, notes = decide_operation(match, doc, doc.charges[0], settings)
    assert operation == Operation.CREATE
    assert merged.charge_details.amount == 40.0


def test_l2_requires_currency(settings, tmp_path):
    repo = load_bills([_write(tmp_path / "min.json", _minimal_bill())], settings)
    doc = _doc(ref=None, bl="OOLU4055331281")
    doc.charges[0].currency = None
    match = match_charge(doc, doc.charges[0], repo, settings)
    assert match.level.value != "L2"


def test_coverage_stats(settings, tmp_path):
    _write(tmp_path / "ext.json", _extended_bill())
    _write(tmp_path / "min.json", _minimal_bill(bill_id="BILL-MIN"))
    repo = load_bills([tmp_path], settings)
    coverage = repo.coverage()
    assert coverage.extended == 0.5
    assert coverage.minimal == 0.5
    assert coverage.L1_ready == 1.0
    assert coverage.L2_ready == 1.0
