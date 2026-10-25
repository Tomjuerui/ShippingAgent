"""文本型 PDF 解析（FR-03）：先抽文本层，内容过少由调用方转 OCR。"""

from __future__ import annotations

import io

from app.agent.nodes.unpack_eml import Payload
from app.extraction.schemas import UIRBlock, UIRDocument, UIRTable


def _looks_header(row: list[str]) -> bool:
    joined = " ".join(row).lower()
    return any(
        keyword in joined
        for keyword in ("charge", "description", "amount", "currency", "费用", "金额", "币种")
    )


def parse_textpdf(
    payload: Payload, min_chars: int = 40, uir_id: str | None = None
) -> UIRDocument | None:
    import pdfplumber

    uir_id = uir_id or f"uir-{payload.sha256[:10]}-pdf"
    blocks: list[UIRBlock] = []
    warnings: list[str] = []
    with pdfplumber.open(io.BytesIO(payload.data)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                blocks.append(
                    UIRBlock(
                        block_id=f"{uir_id}-p{page_number}-text",
                        kind="text",
                        text=text,
                        locator={"page": page_number, "bbox": list(page.bbox)},
                    )
                )
            try:
                tables = page.extract_tables() or []
            except Exception as exc:
                tables = []
                warnings.append(f"第 {page_number} 页表格抽取失败：{exc}")
            for table_index, table in enumerate(tables, start=1):
                matrix = [
                    [" ".join((cell or "").split()) for cell in row]
                    for row in table
                    if row and any((cell or "").strip() for cell in row)
                ]
                if not matrix:
                    continue
                if _looks_header(matrix[0]):
                    headers, rows = matrix[0], matrix[1:]
                else:
                    headers, rows = [], matrix
                blocks.append(
                    UIRBlock(
                        block_id=f"{uir_id}-p{page_number}-t{table_index}",
                        kind="table",
                        table=UIRTable(
                            table_id=f"{uir_id}-p{page_number}-t{table_index}",
                            headers=headers,
                            rows=rows,
                            locator={"page": page_number, "table": table_index},
                        ),
                        locator={"page": page_number, "table": table_index},
                    )
                )
    total_text = sum(len(block.text or "") for block in blocks if block.kind == "text")
    total_rows = sum(len(block.table.rows) for block in blocks if block.table)
    if total_text < min_chars and total_rows == 0:
        return None
    return UIRDocument(
        uir_id=uir_id,
        source=payload.source,
        content_node_id=payload.node_id,
        blocks=blocks,
        parse_mode="pdf-text",
        parse_quality="high" if total_text >= min_chars else "low",
        warnings=warnings,
    )
