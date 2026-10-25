"""OCR 客户端（FR-03 / WP-03）：重试、降级、原文录制、容错响应归一。"""

from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from app.config.settings import Settings
from app.extraction.schemas import SourceInfo, UIRBlock, UIRDocument, UIRTable
from app.storage.file_exposer import ExposeError, FileExposer

_TEXT_KEYS = ("text", "content", "raw_text", "ocr_text", "markdown", "md", "plain_text", "transcription")
_LINE_CONTAINERS = ("lines", "blocks", "paragraphs", "items", "results", "result", "data")
_TABLE_CONTAINERS = ("tables", "table")
_BBOX_KEYS = ("bbox", "box", "position", "coordinates", "bounding_box")
_CONF_KEYS = ("confidence", "score", "probability", "avg_confidence")


class OCRUnavailable(RuntimeError):
    def __init__(self, message: str, raw: Any = None):
        super().__init__(message)
        self.raw = raw


class OCRLowQuality(RuntimeError):
    def __init__(self, message: str, result: "OCRResult | None" = None):
        super().__init__(message)
        self.result = result


@dataclass
class OCRLine:
    text: str
    page: int = 1
    bbox: list[float] | None = None
    confidence: float | None = None


@dataclass
class OCRTable:
    rows: list[list[str]] = field(default_factory=list)
    page: int = 1
    bbox: list[float] | None = None


@dataclass
class OCRResult:
    text: str
    lines: list[OCRLine] = field(default_factory=list)
    tables: list[OCRTable] = field(default_factory=list)
    confidence: float | None = None
    raw: Any = None

    def page_text(self, page: int) -> str:
        return "\n".join(line.text for line in self.lines if line.page == page)


def _as_bbox(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return [float(v) for v in value[:4]]
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        keys = ("x0", "top", "x1", "bottom")
        if all(k in value for k in keys):
            try:
                return [float(value[k]) for k in keys]
            except (TypeError, ValueError):
                return None
        if all(k in value for k in ("x", "y", "width", "height")):
            try:
                x, y = float(value["x"]), float(value["y"])
                return [x, y, x + float(value["width"]), y + float(value["height"])]
            except (TypeError, ValueError):
                return None
    return None


def _as_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1:
            number /= 100.0
        return max(0.0, min(1.0, number))
    return None


def _cell_text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return " ".join(cell.split())
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        for key in _TEXT_KEYS + ("value", "word"):
            if key in cell and isinstance(cell[key], (str, int, float)):
                return " ".join(str(cell[key]).split())
        for key in ("cell", "data"):
            if key in cell:
                return _cell_text(cell[key])
        return ""
    if isinstance(cell, (list, tuple)):
        return " ".join(filter(None, (_cell_text(item) for item in cell)))
    return str(cell)


def _table_rows(node: dict) -> list[list[str]]:
    raw_rows = node.get("rows") or node.get("table")
    if isinstance(raw_rows, dict):
        raw_rows = raw_rows.get("rows")
    if isinstance(raw_rows, list):
        rows = []
        for row in raw_rows:
            if isinstance(row, dict):
                row = row.get("cells") or row.get("row") or row.get("values") or []
            if isinstance(row, list):
                rows.append([_cell_text(cell) for cell in row])
        if rows:
            return rows
    cells = node.get("cells")
    if isinstance(cells, list):
        positioned: list[tuple[int, int, str]] = []
        for index, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            row = cell.get("row", cell.get("row_index", 0))
            col = cell.get("col", cell.get("column", cell.get("col_index", index)))
            try:
                positioned.append((int(row), int(col), _cell_text(cell)))
            except (TypeError, ValueError):
                continue
        if positioned:
            max_row = max(row for row, _, _ in positioned)
            max_col = max(col for _, col, _ in positioned)
            matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
            for row, col, text in positioned:
                matrix[row][col] = text
            return matrix
    return []


def _line_from(node: dict) -> OCRLine | None:
    text = next(
        (str(node[key]).strip() for key in _TEXT_KEYS if isinstance(node.get(key), str)),
        "",
    )
    if not text:
        return None
    bbox = next((_as_bbox(node[key]) for key in _BBOX_KEYS if node.get(key)), None)
    confidence = next(
        (_as_confidence(node[key]) for key in _CONF_KEYS if node.get(key) is not None), None
    )
    return OCRLine(text=text, bbox=bbox, confidence=confidence)


def parse_ocr_payload(payload: Any) -> OCRResult:
    """把厂商/自建 OCR 的多样返回结构归一为文本、行、表格、置信度。"""
    if isinstance(payload, str):
        return OCRResult(text=payload.strip(), raw=payload)
    if isinstance(payload, dict) and "data" in payload:
        meta_keys = {"data", "success", "message", "error_code", "task_id"}
        if set(payload).issubset(meta_keys):
            payload = payload.get("data")

    lines: list[OCRLine] = []
    tables: list[OCRTable] = []
    confidences: list[float] = []
    seen: set[tuple[int, str]] = set()

    def add_line(text: str, page: int, bbox=None, confidence=None) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        key = (page, text)
        if key in seen:
            return
        seen.add(key)
        lines.append(OCRLine(text=text, page=page, bbox=bbox, confidence=confidence))
        if confidence is not None:
            confidences.append(confidence)

    def walk(node: Any, page: int = 1, depth: int = 0) -> None:
        if depth > 12 or node is None:
            return
        if isinstance(node, str):
            add_line(node, page)
            return
        if isinstance(node, (int, float, bool)):
            return
        if isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, dict) and "page" in item:
                    try:
                        child_page = int(item["page"])
                    except (TypeError, ValueError):
                        child_page = page
                    walk(item, child_page, depth + 1)
                else:
                    walk(item, page, depth + 1)
            return
        if not isinstance(node, dict):
            return

        confidence = next(
            (_as_confidence(node[key]) for key in _CONF_KEYS if node.get(key) is not None), None
        )
        if confidence is not None:
            confidences.append(confidence)

        if any(key in node for key in ("rows", "cells", "table")) and (
            isinstance(node.get("rows"), list)
            or isinstance(node.get("cells"), list)
            or isinstance(node.get("table"), (list, dict))
        ):
            rows = _table_rows(node)
            if rows:
                tables.append(OCRTable(rows=rows, page=page, bbox=_as_bbox(node.get("bbox"))))
                return

        for key in _TABLE_CONTAINERS:
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    walk(item, page, depth + 1)
            elif isinstance(value, dict):
                walk(value, page, depth + 1)

        if "pages" in node and isinstance(node["pages"], list):
            for index, page_node in enumerate(node["pages"], start=1):
                if isinstance(page_node, dict):
                    try:
                        child_page = int(page_node.get("page", page_node.get("page_no", index)))
                    except (TypeError, ValueError):
                        child_page = index
                    walk(page_node, child_page, depth + 1)
                else:
                    walk(page_node, index, depth + 1)
            return

        if "words" in node and isinstance(node["words"], list):
            words = [
                _cell_text(word) for word in node["words"]
                if isinstance(word, (str, dict)) and _cell_text(word)
            ]
            if words:
                add_line(" ".join(words), page, _as_bbox(node.get("bbox")), confidence)
            return

        container_found = False
        for key in _LINE_CONTAINERS:
            value = node.get(key)
            if isinstance(value, (list, dict)):
                container_found = True
                walk(value, page, depth + 1)
        for key in ("lines", "words", "blocks", "paragraphs"):
            value = node.get(key)
            if isinstance(value, list):
                container_found = True
                walk(value, page, depth + 1)

        line = _line_from(node)
        if line and not container_found:
            add_line(line.text, page, line.bbox, line.confidence)
        elif line and "text" in node and not any(key in node for key in ("lines", "words")):
            add_line(line.text, page, line.bbox, line.confidence)

    walk(payload)
    page_numbers = sorted({line.page for line in lines})
    text = "\n".join(
        "\n".join(line.text for line in lines if line.page == page) for page in page_numbers
    )
    if not text and tables:
        text = "\n".join(" | ".join(row) for table in tables for row in table.rows)
    average = sum(confidences) / len(confidences) if confidences else None
    return OCRResult(text=text, lines=lines, tables=tables, confidence=average, raw=payload)


def ocr_result_to_uir(
    result: OCRResult,
    source: SourceInfo,
    node_id: str | None,
    uir_id: str,
    min_chars: int = 40,
) -> UIRDocument:
    blocks: list[UIRBlock] = []
    pages = sorted({line.page for line in result.lines})
    for page in pages or [1]:
        text = result.page_text(page) if result.lines else (result.text if page == 1 else "")
        if text.strip():
            page_lines = [line for line in result.lines if line.page == page and line.bbox]
            bbox = None
            if page_lines:
                bboxes = [line.bbox for line in page_lines if line.bbox]
                bbox = [
                    min(b[0] for b in bboxes),
                    min(b[1] for b in bboxes),
                    max(b[2] for b in bboxes),
                    max(b[3] for b in bboxes),
                ]
            blocks.append(
                UIRBlock(
                    block_id=f"{uir_id}-p{page}",
                    kind="text",
                    text=text,
                    locator={"page": page, "bbox": bbox},
                    confidence=result.confidence,
                )
            )
    for index, table in enumerate(result.tables, start=1):
        if not table.rows:
            continue
        header = table.rows[0]
        joined = " ".join(header).lower()
        looks_header = any(
            keyword in joined
            for keyword in ("charge", "description", "amount", "currency", "费用", "金额", "币种")
        )
        headers, rows = (header, table.rows[1:]) if looks_header else ([], table.rows)
        blocks.append(
            UIRBlock(
                block_id=f"{uir_id}-t{index}",
                kind="table",
                table=UIRTable(
                    table_id=f"{uir_id}-t{index}",
                    headers=headers,
                    rows=rows,
                    locator={"page": table.page, "table": index},
                ),
                locator={"page": table.page, "table": index, "bbox": table.bbox},
                confidence=result.confidence,
            )
        )
    warnings = []
    if len(result.text.strip()) < min_chars and not result.tables:
        warnings.append("OCR 文本过少，疑似低质量识别")
    return UIRDocument(
        uir_id=uir_id,
        source=source,
        content_node_id=node_id,
        blocks=blocks,
        parse_mode="ocr",
        parse_quality="low" if warnings else "high",
        warnings=warnings,
    )


class OCRClient:
    def __init__(
        self,
        settings: Settings,
        exposer: FileExposer | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff: tuple[float, ...] = (1.0, 4.0, 16.0),
        record: bool | None = None,
    ):
        self.settings = settings
        self.exposer = exposer or FileExposer(settings, session=session)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.backoff = backoff
        self.record = record

    def process(self, path: str | Path, content_type: str | None = None) -> OCRResult:
        path = Path(path)
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        exposed = None
        try:
            exposed = self.exposer.expose(path, ctype)
        except ExposeError as exc:
            raise OCRUnavailable(f"文件外发失败：{exc}") from exc

        attempts = max(1, self.settings.ocr.retries)
        last_error: Exception | None = None
        try:
            for attempt in range(attempts):
                try:
                    response = self.session.post(
                        self.settings.ocr.url,
                        json={
                            "file": exposed.url,
                            "task": self.settings.ocr.task,
                            "callBack": "",
                            "taskType": "",
                        },
                        timeout=(
                            self.settings.ocr.connect_timeout,
                            self.settings.ocr.read_timeout,
                        ),
                    )
                    if response.status_code >= 500 or response.status_code == 429:
                        raise OCRUnavailable(f"OCR HTTP {response.status_code}")
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("success") is False:
                        raise OCRUnavailable(
                            f"OCR 返回失败：{payload.get('message') or payload.get('error_code')}",
                            raw=payload,
                        )
                    result = parse_ocr_payload(payload)
                    if self.record:
                        self._record(path, payload)
                    if (
                        len(result.text.strip()) < self.settings.ocr.min_text_chars
                        and not result.tables
                    ):
                        raise OCRLowQuality(
                            f"OCR 文本长度 {len(result.text.strip())} 低于阈值", result
                        )
                    return result
                except (OCRLowQuality, OCRUnavailable) as exc:
                    last_error = exc
                    if isinstance(exc, OCRLowQuality):
                        raise
                    if attempt < attempts - 1:
                        self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                except Exception as exc:
                    last_error = exc
                    if attempt < attempts - 1:
                        self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            raise OCRUnavailable(f"OCR 重试 {attempts} 次仍失败：{last_error}", raw=None)
        finally:
            if exposed:
                exposed.close()

    def _record(self, path: Path, payload: Any) -> None:
        directory = self.settings.vcr_dir / "ocr"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{path.stem}-{abs(hash(path.name)) % 10**8:08d}.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
