"""Word 解析（FR-05）：DOCX 段落 + 表格；DOC 二进制文本降级。"""

from __future__ import annotations

import io
import re

from app.agent.nodes.unpack_eml import Payload
from app.extraction.schemas import UIRBlock, UIRDocument, UIRTable

_PRINTABLE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff ,.;:/()\-$%&'\"@]{3,}")


def _cell_text(cell) -> str:
    return " ".join((cell.text or "").split())


def _table_headers(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    if not rows:
        return [], []
    first = rows[0]
    non_empty = [cell for cell in first if cell]
    looks_header = bool(non_empty) and len(non_empty) >= max(2, len(first) // 2)
    if looks_header:
        return first, rows[1:]
    return [], rows


def _doc_binary_text(data: bytes) -> str:
    """DOC（OLE）无纯 Python 结构化解析器时的保守文本提取。"""
    candidates: list[str] = []
    for encoding in ("utf-16-le", "gb18030", "cp1252"):
        try:
            text = data.decode(encoding, errors="ignore")
        except LookupError:  # pragma: no cover
            continue
        runs = _PRINTABLE.findall(text)
        if runs:
            candidates.append("\n".join(run.strip() for run in runs if run.strip()))
    return max(candidates, key=len, default="")


def parse_word(payload: Payload, uir_id: str | None = None) -> UIRDocument:
    uir_id = uir_id or f"uir-{payload.sha256[:10]}-word"
    blocks: list[UIRBlock] = []
    warnings: list[str] = []
    quality = "high"

    if payload.file_type == "docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = Document(io.BytesIO(payload.data))
        paragraph_index = 0
        table_index = 0
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                paragraph = Paragraph(child, document)
                text = (paragraph.text or "").strip()
                paragraph_index += 1
                if text:
                    blocks.append(
                        UIRBlock(
                            block_id=f"{uir_id}-p{paragraph_index}",
                            kind="text",
                            text=text,
                            locator={"paragraph": paragraph_index},
                        )
                    )
            elif child.tag.endswith("}tbl"):
                table = Table(child, document)
                matrix = [
                    [_cell_text(cell) for cell in row.cells] for row in table.rows
                ]
                matrix = [row for row in matrix if any(cell for cell in row)]
                table_index += 1
                if not matrix:
                    continue
                headers, rows = _table_headers(matrix)
                blocks.append(
                    UIRBlock(
                        block_id=f"{uir_id}-t{table_index}",
                        kind="table",
                        table=UIRTable(
                            table_id=f"{uir_id}-t{table_index}",
                            headers=headers,
                            rows=rows,
                            locator={"table": table_index},
                        ),
                        locator={"table": table_index},
                    )
                )
    elif payload.file_type == "doc":
        text = _doc_binary_text(payload.data)
        quality = "low"
        warnings.append("DOC 旧格式仅做文本流提取，表格结构可能丢失，建议提供 DOCX")
        if not text:
            warnings.append("DOC 未提取到可读文本")
        for index, line in enumerate(text.splitlines()):
            if line.strip():
                blocks.append(
                    UIRBlock(
                        block_id=f"{uir_id}-l{index + 1}",
                        kind="text",
                        text=line.strip(),
                        locator={"line": index + 1},
                    )
                )
    else:
        raise ValueError(f"不支持的 Word 类型：{payload.file_type}")

    if not blocks:
        warnings.append("Word 未解析出任何文本或表格")
    return UIRDocument(
        uir_id=uir_id,
        source=payload.source,
        content_node_id=payload.node_id,
        blocks=blocks,
        parse_mode="word",
        parse_quality=quality,
        warnings=warnings,
    )
