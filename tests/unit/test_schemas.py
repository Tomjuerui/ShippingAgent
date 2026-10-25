"""契约冻结断言（WP-01 DoD）。"""

import json

import pytest

from app.config.settings import load
from app.extraction.schemas import (
    BillRecord,
    ChargeLine,
    DocumentExtraction,
    FinancialAttributesOut,
    Operation,
    UIRBlock,
    UIRDocument,
    UIRTable,
    SourceInfo,
)


def test_llm_output_schema_excludes_deterministic_fields():
    fields = set(DocumentExtraction.model_fields)
    for forbidden in ("posting_type", "operation", "charge_item", "standardized_currency"):
        assert forbidden not in fields
    charge_fields = set(ChargeLine.model_fields)
    assert "posting_type" not in charge_fields
    assert "operation" not in charge_fields
    assert "charge_item" not in charge_fields


def test_charge_number_coercion():
    line = ChargeLine(
        original_charge_name="H/D Fee",
        quantity="2",
        unit_price="20.00",
        amount="US$ 1,240.00",
        currency="US$",
        payment_term="PREPAID",
    )
    assert line.quantity == 2.0
    assert line.unit_price == 20.0
    assert line.amount == 1240.0
    assert line.currency == "US$"


def test_parentheses_negative_and_ocr_digits():
    assert ChargeLine(amount="(40.00)").amount == -40.0
    assert ChargeLine(amount="4O.00").amount == 40.0
    assert ChargeLine(amount="1.240.00").amount == 1240.0


def test_confidence_clamped():
    line = ChargeLine(confidence={"amount": 1.4, "currency": -0.2})
    assert line.confidence == {"amount": 1.0, "currency": 0.0}


def test_uir_full_text_includes_tables():
    uir = UIRDocument(
        uir_id="u1",
        source=SourceInfo(file_name="a.pdf", file_type="pdf"),
        blocks=[
            UIRBlock(block_id="b1", kind="text", text="Debit Note"),
            UIRBlock(
                block_id="b2",
                kind="table",
                table=UIRTable(
                    table_id="t1",
                    headers=["Charge", "Amount"],
                    rows=[["THC", "40.00"]],
                ),
            ),
        ],
    )
    assert "Debit Note" in uir.full_text()
    assert "THC | 40.00" in uir.full_text()


def test_bill_record_json_roundtrip():
    record = BillRecord(
        record_id="abc-001",
        operation=Operation.CREATE,
        financial_attributes=FinancialAttributesOut(
            accounting_entity="Origin Agent", posting_type="AP"
        ),
    )
    payload = record.model_dump(mode="json")
    assert payload["operation"] == "CREATE"
    assert json.loads(json.dumps(payload))["record_id"] == "abc-001"


def test_settings_load_and_env_override(monkeypatch):
    settings = load()
    assert settings.accounting_entity.name == "Origin Agent"
    assert settings.llm.active_profile().model
    assert settings.output_dir.is_absolute()

    monkeypatch.setenv("LLM_MODEL", "unit-test-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid/v1/")
    profile = settings.llm.active_profile()
    assert profile.model == "unit-test-model"
    assert profile.base_url == "http://example.invalid/v1"


def test_settings_rejects_unknown_profile(monkeypatch):
    settings = load()
    monkeypatch.setenv("LLM_PROFILE", "not-exist")
    with pytest.raises(KeyError):
        settings.llm.active_profile()
