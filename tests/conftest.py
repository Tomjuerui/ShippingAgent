"""测试夹具：临时配置与样例文档生成。"""

from __future__ import annotations

import email
from email.message import EmailMessage
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.config.settings import load


@pytest.fixture
def settings(tmp_path):
    s = load()
    s.paths.output_dir = tmp_path / "output"
    s.paths.data_dir = tmp_path / "data"
    s.llm.cache_dir = str(tmp_path / "cache")
    s.llm.cache_enabled = False
    s.ensure_dirs()
    return s


def make_excel(path: Path, header_offset_rows: int = 0, merged: bool = False) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "DN"
    for _ in range(header_offset_rows):
        ws.append(["某货代对账单", None, None, None, None, None])
    if merged:
        ws.append(["Charge Description", None, "Qty", "Unit Price", "Amount", "Ccy"])
        ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=2)
        ws.append(["H/D Fee", "", 2, 20.0, 40.0, "USD"])
        ws.append(["THC", "", 1, 100.0, 100.0, "USD"])
        ws.append(["Subtotal", "", None, None, 140.0, "USD"])
    else:
        ws.append(["Charge Description", "Qty", "Unit Price", "Amount", "Ccy", "Term"])
        ws.append(["H/D Fee", 2, 20.0, 40.0, "USD", "PREPAID"])
        ws.append(["THC", 1, 100.0, 100.0, "USD", "CC"])
        ws.append(["Subtotal", None, None, 140.0, "USD", None])
    wb.save(path)
    return path


def make_word(path: Path) -> Path:
    from docx import Document

    document = Document()
    document.add_paragraph("DEBIT NOTE DN-2026-001")
    document.add_paragraph("Issuer: Destination Agent")
    document.add_paragraph("Recipient: Origin Agent")
    table = document.add_table(rows=3, cols=5)
    headers = ["Charge Description", "Qty", "Unit Price", "Amount", "Currency"]
    for index, value in enumerate(headers):
        table.rows[0].cells[index].text = value
    values = [["H/D Fee", "2", "20.00", "40.00", "USD"], ["Total", "", "", "40.00", "USD"]]
    for row_index, row in enumerate(values, start=1):
        for col_index, value in enumerate(row):
            table.rows[row_index].cells[col_index].text = value
    document.save(path)
    return path


def make_eml(path: Path, attachment: Path | None = None, html_body: str | None = None) -> Path:
    message = EmailMessage()
    message["Subject"] = "Debit Note DN-2026-001"
    message["From"] = "billing@destination-agent.com"
    message["To"] = "ap@origin-agent.com"
    message["Message-ID"] = "<dn-2026-001@destination-agent.com>"
    message["Date"] = "Mon, 20 Oct 2026 10:00:00 +0800"
    if html_body:
        message.set_content("Please see the attached debit note.")
        message.add_alternative(html_body, subtype="html")
    else:
        message.set_content("Please see the attached debit note.")
    if attachment:
        maintype, subtype = {
            "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "pdf": ("application", "pdf"),
            "docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
        }.get(attachment.suffix.lstrip(".").lower(), ("application", "octet-stream"))
        message.add_attachment(
            attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name
        )
    path.write_bytes(bytes(message))
    return path


def make_nested_eml(path: Path, inner_attachment: Path) -> Path:
    inner = EmailMessage()
    inner["Subject"] = "Inner Debit Note"
    inner["From"] = "inner@destination-agent.com"
    inner["Message-ID"] = "<inner@destination-agent.com>"
    inner.set_content("Inner body")
    inner.add_attachment(
        inner_attachment.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=inner_attachment.name,
    )
    outer = EmailMessage()
    outer["Subject"] = "Fwd: Inner Debit Note"
    outer["From"] = "forwarder@example.com"
    outer["Message-ID"] = "<outer@example.com>"
    outer.set_content("Forwarding the debit note.")
    outer.add_attachment(inner, filename="inner.eml")
    path.write_bytes(bytes(outer))
    return path


def make_pdf(path: Path, text: str = "DEBIT NOTE DN-2026-001 H/D Fee 2 20.00 40.00 USD") -> Path:
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    header = b"%PDF-1.4\n"
    body = b""
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(header) + len(body))
        body += f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref_offset = len(header) + len(body)
    xref = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(
        f"{offset:010d} 00000 n \n" for offset in offsets
    )
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    )
    path.write_bytes(header + body + xref.encode("latin-1") + trailer.encode("latin-1"))
    return path
