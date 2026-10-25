"""解析层单测（WP-02 DoD）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.nodes.ingest import detect_format, discover_inputs, ingest_file
from app.agent.nodes.parse_excel import parse_excel
from app.agent.nodes.parse_textpdf import parse_textpdf
from app.agent.nodes.parse_word import parse_word
from app.agent.nodes.unpack_eml import Payload, html_to_text_and_tables, unpack_file
from tests.conftest import make_eml, make_excel, make_nested_eml, make_pdf, make_word

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _payload(path: Path, file_type: str) -> Payload:
    from app.agent.nodes.ingest import ingest_file
    from app.extraction.schemas import SourceInfo

    item = ingest_file(path)
    return Payload(
        node_id="n1",
        file_name=path.name,
        file_type=file_type,
        data=path.read_bytes(),
        path=path,
        sha256=item.sha256,
        source=SourceInfo(file_name=path.name, file_type=file_type, file_sha256=item.sha256),
    )


def test_magic_bytes_override_wrong_extension(tmp_path):
    path = tmp_path / "looks_like.pdf"
    path.write_bytes(_PNG)
    item = ingest_file(path)
    assert item.detected_type == "png"
    assert any("不一致" in warning for warning in item.warnings)


def test_ingest_sha256_stable(tmp_path):
    path = make_excel(tmp_path / "a.xlsx")
    assert ingest_file(path).sha256 == ingest_file(path).sha256


def test_discover_skips_json_and_unknown(tmp_path):
    make_excel(tmp_path / "a.xlsx")
    (tmp_path / "bills.json").write_text("{}", encoding="utf-8")
    (tmp_path / "evil.bin").write_bytes(b"\x00\x01\x02")
    ingested, messages = discover_inputs([tmp_path])
    assert [item.file_name for item in ingested] == ["a.xlsx"]
    assert any("JSON" in message for message in messages)
    assert any("不支持" in message for message in messages)


def test_eml_body_and_attachment(tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    eml = make_eml(tmp_path / "mail.eml", attachment=excel)
    item = ingest_file(eml)
    result = unpack_file(item, tmp_path / "work")
    types = {payload.file_type for payload in result.payloads}
    assert "eml_body" in types
    assert "xlsx" in types
    body = next(payload for payload in result.payloads if payload.is_body)
    assert "attached debit note" in body.data.decode("utf-8")
    assert body.source.email_subject == "Debit Note DN-2026-001"


def test_html_table_in_body(tmp_path):
    html = "<html><body><p>Bill</p><table><tr><th>Charge</th><th>Amount</th></tr><tr><td>THC</td><td>40.00</td></tr></table></body></html>"
    eml = make_eml(tmp_path / "html.eml", html_body=html)
    result = unpack_file(ingest_file(eml), tmp_path / "work")
    body = next(payload for payload in result.payloads if payload.is_body)
    assert body.body_tables
    assert body.body_tables[0][1] == ["THC", "40.00"]


def test_html_collector_ignores_script():
    text, tables = html_to_text_and_tables(
        "<p>Hello</p><script>alert('x')</script><table><tr><td>A</td></tr></table>"
    )
    assert "Hello" in text
    assert "alert" not in text
    assert tables == [[["A"]]]


def test_nested_eml_depth_limit(tmp_path):
    pdf = make_pdf(tmp_path / "inner.pdf")
    nested = make_nested_eml(tmp_path / "outer.eml", pdf)
    shallow = unpack_file(ingest_file(nested), tmp_path / "w1", max_depth=1)
    assert not any(payload.file_type == "pdf" for payload in shallow.payloads)
    deep = unpack_file(ingest_file(nested), tmp_path / "w2", max_depth=3)
    assert any(payload.file_type == "pdf" for payload in deep.payloads)


def test_excel_header_offset_and_merged_cells(tmp_path):
    path = make_excel(tmp_path / "offset.xlsx", header_offset_rows=2, merged=True)
    uir = parse_excel(_payload(path, "xlsx"))
    tables = [block.table for block in uir.blocks if block.kind == "table"]
    assert tables
    table = tables[0]
    assert "Charge Description" in table.headers[0]
    assert table.locator["header_row"] == 3
    assert any(row[0] == "H/D Fee" for row in table.rows)
    assert any("对账单" in (block.text or "") for block in uir.blocks if block.kind == "text")


def test_excel_numeric_cells_not_cast_to_string_with_precision_loss(tmp_path):
    path = make_excel(tmp_path / "num.xlsx")
    uir = parse_excel(_payload(path, "xlsx"))
    text = uir.full_text()
    assert "40" in text and "100" in text


def test_word_paragraphs_and_tables(tmp_path):
    path = make_word(tmp_path / "bill.docx")
    uir = parse_word(_payload(path, "docx"))
    assert any("DEBIT NOTE" in (block.text or "") for block in uir.blocks if block.kind == "text")
    tables = [block.table for block in uir.blocks if block.kind == "table"]
    assert tables and tables[0].headers[0] == "Charge Description"
    assert tables[0].rows[0][0] == "H/D Fee"


def test_legacy_doc_text_fallback(tmp_path):
    path = tmp_path / "legacy.doc"
    path.write_bytes("DEBIT NOTE DN-2026-001 H/D Fee 40.00 USD".encode("utf-16-le"))
    payload = _payload(path, "doc")
    payload.file_type = "doc"
    uir = parse_word(payload)
    assert uir.parse_quality == "low"
    assert "DN-2026-001" in uir.full_text()


def test_text_pdf_extracts_text(tmp_path):
    path = make_pdf(tmp_path / "bill.pdf", "DEBIT NOTE DN-2026-001 H/D Fee 40.00 USD")
    uir = parse_textpdf(_payload(path, "pdf"), min_chars=10)
    assert uir is not None
    assert "DEBIT NOTE" in uir.full_text()


def test_scanned_pdf_without_text_returns_none(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", "")
    assert parse_textpdf(_payload(path, "pdf"), min_chars=10) is None
