"""缺陷修复回归（集成层）：空提取守卫、账本合并、主体补救、批级重试、消息一致性。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.graph import run_batch
from app.agent.nodes.ingest import ingest_file
from app.extraction.llm_client import LLMClient, LLMUnavailable
from app.extraction.schemas import DocumentExtraction, Operation
from app.server import create_app
from app.storage.outputs import load_ledger
from tests.conftest import make_excel, make_pdf, make_word
from tests.fakes import KeywordBackend, extraction_dn

EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _bill(amount=35.0, issuer="Destination Agent", recipient="Origin Agent") -> dict:
    return {
        "bill_id": "BILL-000123",
        "version": 3,
        "document_info": {
            "document_type": "Debit Note",
            "reference_no": "DN-2026-001",
            "document_date": "2026-09-20",
        },
        "shipment_info": {
            "bl_no": "OOLU4055331281",
            "booking_no": "BK-2026-889",
            "container_no": "TIIU8975975",
        },
        "transaction_party": {"issuer": issuer, "recipient": recipient},
        "charges": [
            {
                "charge_item": "Handling Charge",
                "original_charge_name": "H/D Fee",
                "quantity": 2,
                "unit_price": 20.0,
                "amount": amount,
                "currency": "USD",
                "payment_term": "PP",
            }
        ],
        "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
    }


def _run(settings, inputs, bills=None, extraction=None, backend=None, batch_id=None, **kwargs):
    backend = backend or KeywordBackend([("H/D Fee", extraction or extraction_dn())])
    client = LLMClient(settings, backend=backend, use_cache=False)
    batch = run_batch(
        inputs,
        bill_paths=bills,
        settings=settings,
        batch_id=batch_id,
        llm_client=client,
        ignore_ledger=kwargs.pop("ignore_ledger", True),
        deterministic=kwargs.pop("deterministic", False),
        **kwargs,
    )
    return batch, backend


# --------------------------------------------------------------------------- #
# P1-4 空提取守卫
# --------------------------------------------------------------------------- #
def test_empty_extraction_with_amounts_creates_placeholder_review(settings, tmp_path):
    """识别到单据但零费用、原文含金额 → EXTRACTION_EMPTY 占位复核记录，且不进账本。"""
    pdf = make_pdf(
        tmp_path / "bill.pdf",
        "DEBIT NOTE DC-108478 PILOT AISHIP\n"
        "HANDLING CHARGE 1 65.00 USD C\n"
        "TOTAL AMOUNT 1,410.40 USD\n",
    )
    empty_with_id = DocumentExtraction(document_type="DEBIT NOTE", reference_no="DC-108478")
    backend = KeywordBackend(rules=[], default=empty_with_id)
    batch, _ = _run(settings, [pdf], backend=backend, batch_id="b-empty")
    assert batch.summary.records == 1
    record = batch.records[0]
    assert record.operation == Operation.PENDING_REVIEW
    assert record.review_reason == "EXTRACTION_EMPTY"
    assert record.document_info.reference_no == "DC-108478"
    assert record.charge_details.amount is None
    assert any("空提取" in line for line in record.audit_trail.ai_reasoning)
    file_out = batch.files[0]
    assert file_out.status == "completed"
    assert file_out.review_reason == "EXTRACTION_EMPTY"
    # 占位记录不得写入幂等账本
    assert ingest_file(pdf).sha256 not in load_ledger(settings.output_dir)


def test_empty_extraction_without_amounts_stays_silent(settings, tmp_path):
    """非账单内容（无金额特征）保持原静默行为。"""
    pdf = make_pdf(
        tmp_path / "notice.pdf",
        "PRE-ALERT FOR SHIPMENT SSZSE\nVESSEL CALLING YANTIAN NEXT MONDAY\nNO CHARGES ATTACHED\n",
    )
    empty_with_id = DocumentExtraction(document_type="PRE-ALERT", reference_no="SSZSE")
    backend = KeywordBackend(rules=[], default=empty_with_id)
    batch, _ = _run(settings, [pdf], backend=backend, batch_id="b-silent")
    assert batch.summary.records == 0
    assert batch.files[0].review_reason is None
    assert "未提取到费用行" in (batch.files[0].message or "")


def test_totally_empty_extraction_with_bill_title_creates_placeholder(settings, tmp_path):
    """LLM 一无所获（无类型无编号），但原文具备账单标题+金额特征 → 同样触发守卫。"""
    pdf = make_pdf(
        tmp_path / "bill.pdf",
        "SOME SHIPPING CO\nDEBIT NOTE\nD/C NO. DC-108478\n"
        "HANDLING CHARGE 65.00 USD\nTOTAL AMOUNT 1,410.40 USD\n",
    )
    backend = KeywordBackend(rules=[], default=DocumentExtraction())
    batch, _ = _run(settings, [pdf], backend=backend, batch_id="b-empty2")
    assert batch.summary.records == 1
    record = batch.records[0]
    assert record.operation == Operation.PENDING_REVIEW
    assert record.review_reason == "EXTRACTION_EMPTY"
    assert any("未返回任何单据信息" in line for line in record.audit_trail.ai_reasoning)


# --------------------------------------------------------------------------- #
# P1-5 账本合并写回
# --------------------------------------------------------------------------- #
def test_ignore_ledger_preserves_history(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    word = make_word(tmp_path / "bill.docx")
    sha_excel = ingest_file(excel).sha256
    sha_word = ingest_file(word).sha256
    _run(settings, [excel], batch_id="led-a", ignore_ledger=True)
    _run(settings, [word], batch_id="led-b", ignore_ledger=True)
    ledger = load_ledger(settings.output_dir)
    assert ledger.get(sha_excel) == "led-a"
    assert ledger.get(sha_word) == "led-b"


# --------------------------------------------------------------------------- #
# P2-7 匹配后主体解析补救
# --------------------------------------------------------------------------- #
def test_entity_unresolved_rescued_by_matched_bill_parties(settings, tmp_path):
    """输入双方为 null（ENTITY_UNRESOLVED）但 L1 唯一命中的账单双方可定位主体 → 正常 UPDATE。"""
    excel = make_excel(tmp_path / "bill.xlsx")
    bill_path = tmp_path / "bill.json"
    bill_path.write_text(json.dumps(_bill(35.0)), encoding="utf-8")
    extraction = extraction_dn(issuer=None, recipient=None, amount=40.0)
    batch, _ = _run(settings, [excel], [bill_path], extraction=extraction, batch_id="b-rescue")
    record = batch.records[0]
    assert record.operation == Operation.UPDATE
    assert record.financial_attributes.posting_type == "AP"
    audit = "\n".join(record.audit_trail.ai_reasoning)
    assert "以已有账单双方重算" in audit
    # 双方信息从已有账单继承
    assert record.transaction_party.recipient == "Origin Agent"


# --------------------------------------------------------------------------- #
# P2-6 批级重试
# --------------------------------------------------------------------------- #
class FlakyBackend:
    def __init__(self, extraction):
        self.extraction = extraction
        self.calls = 0

    def structured(self, schema, messages, method):
        self.calls += 1
        if self.calls == 1:
            raise LLMUnavailable("simulated transient outage")
        return self.extraction.model_copy(deep=True)

    def plain(self, messages):
        return json.dumps(self.extraction.model_dump(mode="json"), ensure_ascii=False)


def test_transient_llm_unavailable_retried_once(settings, tmp_path):
    settings.llm.unavailable_retry_seconds = 1
    excel = make_excel(tmp_path / "bill.xlsx")
    backend = FlakyBackend(extraction_dn())
    batch, _ = _run(settings, [excel], backend=backend, batch_id="b-retry")
    assert backend.calls == 2
    assert batch.summary.records == 1
    assert batch.records[0].operation == Operation.CREATE


# --------------------------------------------------------------------------- #
# P3-10 重复提交消息一致性
# --------------------------------------------------------------------------- #
def test_duplicate_message_reports_operation_breakdown(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    _run(settings, [excel], batch_id="dup-a", ignore_ledger=True)
    second, _ = _run(settings, [excel], batch_id="dup-b", ignore_ledger=False)
    message = second.files[0].message or ""
    assert "重复提交（DUPLICATE_SUBMISSION）" in message
    assert "NOCHANGE 1 条" in message
    assert second.records[0].operation == Operation.NOCHANGE


# --------------------------------------------------------------------------- #
# P3-12 勾稽一致性警告
# --------------------------------------------------------------------------- #
def test_update_amount_quantity_price_mismatch_warned(settings, tmp_path):
    """输入只有金额（数量/单价为 null，继承自账单 2×20=40），更新金额 50 后勾稽不一致应告警。"""
    excel = make_excel(tmp_path / "bill.xlsx")
    bill_path = tmp_path / "bill.json"
    bill_path.write_text(json.dumps(_bill(35.0)), encoding="utf-8")
    extraction = extraction_dn(amount=50.0, total=50.0)
    extraction.charges[0].quantity = None
    extraction.charges[0].unit_price = None
    batch, _ = _run(settings, [excel], [bill_path], extraction=extraction, batch_id="b-consist")
    record = batch.records[0]
    assert record.operation == Operation.UPDATE
    assert record.review_reason is None
    audit = "\n".join(record.audit_trail.ai_reasoning)
    assert "不一致" in audit and "勾稽" in audit
    # 数量/单价按规则继承原账单值，未被静默改写
    assert record.charge_details.quantity == 2
    assert record.charge_details.unit_price == 20.0
    assert record.charge_details.amount == 50.0


# --------------------------------------------------------------------------- #
# P2-9 上传接口 bills JSON 校验
# --------------------------------------------------------------------------- #
def test_upload_rejects_malformed_bill_json(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    backend = KeywordBackend([("H/D Fee", extraction_dn())])
    app = create_app(
        settings=settings,
        llm_factory=lambda: LLMClient(settings, backend=backend, use_cache=False),
        inline=True,
    )
    client = TestClient(app)
    response = client.post(
        "/api/batches",
        files=[
            ("files[]", (excel.name, excel.read_bytes(), EXCEL_MIME)),
            ("bills[]", ("bad.json", b"this is not json", "application/json")),
        ],
    )
    assert response.status_code == 400
    assert "不是合法 JSON" in response.json()["detail"]
