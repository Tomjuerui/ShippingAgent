"""单据分块（WP-04）：按页/文档头/长度切分 UIR，供 LLM 逐块提取。"""

from __future__ import annotations

import re

from app.extraction.schemas import ExtractionChunk, UIRBlock, UIRDocument

_DOC_HEADER = re.compile(r"(?i)\b(debit\s*note|credit\s*note|invoice|statement)\b")


def _block_text(block: UIRBlock) -> str:
    if block.kind == "text":
        return (block.text or "").strip()
    if block.kind == "table" and block.table:
        lines = []
        if block.table.headers:
            lines.append(" | ".join(block.table.headers))
        lines.extend(" | ".join(row) for row in block.table.rows)
        return "\n".join(lines)
    return ""


def _header_count(text: str) -> int:
    return len(_DOC_HEADER.findall(text))


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    for line in text.splitlines():
        if len(line) <= limit:
            pieces.append(line)
            continue
        for start in range(0, len(line), limit):
            pieces.append(line[start : start + limit])
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in pieces:
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _make_chunk(
    uir: UIRDocument, index: int, text: str, page: int | None = None
) -> ExtractionChunk:
    return ExtractionChunk(
        chunk_id=f"{uir.uir_id}-c{index:02d}",
        uir_id=uir.uir_id,
        text=text,
        source=uir.source,
        content_node_id=uir.content_node_id,
        page_locator={"page": page} if page is not None else {},
    )


def segment_uir(uir: UIRDocument, max_chars: int = 12000) -> list[ExtractionChunk]:
    blocks = [block for block in uir.blocks if _block_text(block)]
    if not blocks:
        return []

    # 多单据 PDF/OCR：页面级切分，页内文档头作为边界
    page_blocks: dict[int, list[UIRBlock]] = {}
    for block in blocks:
        page = block.locator.get("page")
        if isinstance(page, int):
            page_blocks.setdefault(page, []).append(block)
    page_texts = {
        page: "\n\n".join(_block_text(block) for block in page_blocks[page])
        for page in page_blocks
    }
    total_text = "\n\n".join(page_texts.values())
    if len(page_blocks) > 1 and _header_count(total_text) >= 2:
        groups: list[tuple[list[int], str]] = []
        for page in sorted(page_texts):
            text = page_texts[page]
            if not groups or _header_count(text) >= 1:
                groups.append(([page], text))
            else:
                pages, accumulated = groups[-1]
                groups[-1] = (pages + [page], accumulated + "\n\n" + text)
        chunks: list[ExtractionChunk] = []
        for index, (pages, text) in enumerate(groups, start=1):
            for part_index, part in enumerate(_split_long(text, max_chars), start=1):
                suffix = f"-{part_index}" if part_index > 1 else ""
                chunk = _make_chunk(uir, len(chunks) + 1, part, pages[0] if pages else None)
                if suffix:
                    chunk.chunk_id += suffix
                chunks.append(chunk)
        return chunks

    text = "\n\n".join(_block_text(block) for block in blocks)
    page = blocks[0].locator.get("page") if blocks else None
    parts = _split_long(text, max_chars)
    return [
        _make_chunk(uir, index, part, page if isinstance(page, int) else None)
        for index, part in enumerate(parts, start=1)
    ]
