"""端到端流水线集成测试（WP-08 DoD）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.graph import run_batch
from app.agent.nodes.ingest import ingest_file
from app.extraction.llm_client import LLMClient
from app.extraction.schemas import BatchOutput, BillRecord, Operation
from app.storage.outputs import load_batch_output
from app.storage.status import StatusStore
from tests.conftest import make_eml, make_excel, make_pdf, make_word
from tests.fakes import KeywordBackend, extraction_dn

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _client(settings, extraction=None, backend=None):
    backend = backend or KeywordBackend(
        [("H/D Fee", extraction or extraction_dn())]
    )
    return LLMClient(settings, backend=backend, use_cache=False), backend


def _bill(amount=35.0) -> dict:
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
        "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
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


def _run(settings, inputs, bills=None, extraction=None, batch_id=None, **kwargs):
    client, backend = _client(settings, extraction)
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


def test_excel_update_end_to_end(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    bill_path = tmp_path / "bill.json"
    bill_path.write_text(json.dumps(_bill()), encoding="utf-8")
    batch, _ = _run(settings, [excel], [bill_path], batch_id="b-update")
    assert batch.summary.records == 1
    record = batch.records[0]
    assert record.operation == Operation.UPDATE
    assert record.match_info.match_level == "L1"
    assert record.match_info.matched_bill_id == "BILL-000123"
    assert record.charge_details.amount == 40.0
    assert record.financial_attributes.posting_type == "AP"
    assert record.change_info is not None
    changed = {change.field: change for change in record.change_info.changed_fields}
    assert changed["charge_details.amount"].old_value == 35.0
    assert changed["charge_details.amount"].new_value == 40.0
    audit = "\n".join(record.audit_trail.ai_reasoning)
    assert "原始费用名称 'H/D Fee' 已映射为标准费用项 'Handling Charge'" in audit
    assert "将 charge_details.amount 由 35.00 更新为 40.00" in audit
    assert record.source.file_name == "bill.xlsx"
    BillRecord.model_validate(record.model_dump(mode="json"))
    assert (settings.output_dir / "b-update" / "batch.json").exists()


def test_eml_attachment_uses_root_hash_and_email_metadata(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    eml = make_eml(tmp_path / "mail.eml", attachment=excel)
    root_sha = ingest_file(eml).sha256
    batch, _ = _run(settings, [eml], batch_id="b-eml")
    assert batch.summary.records == 1
    record = batch.records[0]
    assert record.source.file_name == "bill.xlsx"
    assert record.source.file_sha256 == root_sha
    assert record.source.email_subject == "Debit Note DN-2026-001"
    assert record.source.email_message_id == "<dn-2026-001@destination-agent.com>"
    assert record.record_id.startswith(root_sha[:8])


def test_multi_format_batch(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    word = make_word(tmp_path / "bill.docx")
    pdf = make_pdf(tmp_path / "bill.pdf", "DEBIT NOTE DN-2026-001 H/D Fee 2 20.00 40.00 USD")
    batch, _ = _run(settings, [excel, word, pdf], batch_id="b-multi")
    assert batch.summary.input_files == 3
    assert batch.summary.records == 3
    assert {record.operation for record in batch.records} == {Operation.CREATE}
    assert {file.parse_mode for file in batch.files} == {"excel", "word", "pdf-text"}
    assert len({record.record_id for record in batch.records}) == 3


def test_duplicate_file_within_batch_skipped(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    batch, backend = _run(settings, [excel, excel], batch_id="b-dup-batch")
    assert batch.summary.input_files == 2
    assert batch.summary.skipped_files == 1
    assert batch.summary.records == 1
    assert backend.calls == 1
    statuses = {file.file_name: file.status for file in batch.files}
    assert statuses["bill.xlsx"] == "skipped"


def test_duplicate_submission_across_batches_emits_nochange(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    first, backend = _run(settings, [excel], batch_id="b-first")
    assert first.summary.records == 1
    second, backend2 = _run(
        settings, [excel], batch_id="b-second", ignore_ledger=False
    )
    assert backend2.calls == 0
    assert second.summary.skipped_files == 1
    assert second.summary.records == 1
    clone = second.records[0]
    assert clone.operation == Operation.NOCHANGE
    assert any("DUPLICATE_SUBMISSION" in line for line in clone.audit_trail.ai_reasoning)
    assert second.files[0].duplicate_of == "b-first"


def test_idempotent_byte_level_output(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    _run(settings, [excel], batch_id="idem", deterministic=True)
    path = settings.output_dir / "idem" / "batch.json"
    first = path.read_bytes()
    _run(settings, [excel], batch_id="idem", deterministic=True)
    second = path.read_bytes()
    assert first == second
    BatchOutput.model_validate(json.loads(second))


def test_llm_unavailable_marks_file_failed(settings, monkeypatch, tmp_path):
    for key in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings.llm.cache_enabled = False
    excel = make_excel(tmp_path / "bill.xlsx")
    batch = run_batch(
        [excel],
        settings=settings,
        batch_id="b-no-llm",
        ignore_ledger=True,
        deterministic=True,
    )
    assert batch.summary.failed_files == 1
    assert batch.summary.records == 0
    assert batch.files[0].review_reason == "LLM_UNAVAILABLE"


def test_ocr_unavailable_marks_file_failed(settings, tmp_path):
    image = tmp_path / "bill.png"
    image.write_bytes(_PNG)
    client, _ = _client(settings)
    batch = run_batch(
        [image],
        settings=settings,
        batch_id="b-no-ocr",
        llm_client=client,
        ignore_ledger=True,
        deterministic=True,
    )
    assert batch.summary.failed_files == 1
    assert batch.files[0].review_reason == "OCR_UNAVAILABLE"


def test_bad_file_does_not_break_batch(settings, tmp_path):
    excel = make_excel(tmp_path / "good.xlsx")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    batch, _ = _run(settings, [excel, bad], batch_id="b-bad")
    assert batch.summary.input_files == 2
    assert batch.summary.failed_files == 1
    assert batch.summary.records == 1
    statuses = {file.file_name: file.status for file in batch.files}
    assert statuses["good.xlsx"] == "completed"
    assert statuses["bad.pdf"] == "failed"


def test_entity_unresolved_reviews_with_null_posting(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    extraction = extraction_dn(issuer="Unknown Co.", recipient="Other Co.")
    batch, _ = _run(settings, [excel], extraction=extraction, batch_id="b-entity")
    record = batch.records[0]
    assert record.operation == Operation.PENDING_REVIEW
    assert record.review_reason == "ENTITY_UNRESOLVED"
    assert record.financial_attributes.posting_type is None
    assert record.confidence >= settings.thresholds.review_below


def test_sum_mismatch_reviews(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    extraction = extraction_dn(total=999.0)
    batch, _ = _run(settings, [excel], extraction=extraction, batch_id="b-sum")
    record = batch.records[0]
    assert record.operation == Operation.PENDING_REVIEW
    assert record.review_reason == "SUM_MISMATCH"
    assert record.validation.passed is False
    assert record.financial_attributes.posting_type == "AP"


def test_low_confidence_reviews(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    extraction = extraction_dn(confidence=0.5)
    batch, _ = _run(settings, [excel], extraction=extraction, batch_id="b-low")
    record = batch.records[0]
    assert record.operation == Operation.PENDING_REVIEW
    assert record.review_reason == "OTHER"
    assert record.low_confidence is True


def test_two_input_charges_matching_one_bill_line_review(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    base = extraction_dn()
    base.charges = [
        base.charges[0].model_copy(deep=True),
        base.charges[0].model_copy(deep=True),
    ]
    base.charges[0].amount = 65.0
    base.charges[1].amount = 35.0
    bill_path = tmp_path / "bill.json"
    bill_path.write_text(json.dumps(_bill(amount=60.0)), encoding="utf-8")
    batch, _ = _run(
        settings, [excel], [bill_path], extraction=base, batch_id="b-shared-bill"
    )
    assert batch.summary.records == 2
    assert all(record.operation == Operation.PENDING_REVIEW for record in batch.records)
    assert all(record.review_reason == "MATCH_AMBIGUOUS" for record in batch.records)
    assert all(
        any("无法确定唯一对应关系" in line for line in record.audit_trail.ai_reasoning)
        for record in batch.records
    )


def test_status_file_and_operation_counts(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    _run(settings, [excel], batch_id="b-status")
    status = StatusStore(settings.output_dir, "b-status").read()
    assert status is not None
    assert status.status == "completed"
    assert status.progress == 100
    assert status.total_files == 1
    assert status.processed_files == 1
    assert status.operation_counts.CREATE == 1
    output = load_batch_output(settings.output_dir, "b-status")
    assert output is not None and output.summary.records == 1
