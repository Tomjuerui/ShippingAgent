"""EML 递归解包 → 内容树 + 待解析 Payload（FR-02 / 专题 A/B）。"""

from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

from app.agent.nodes.ingest import IngestedFile, detect_format_bytes, sha256_bytes
from app.extraction.schemas import ContentNode, SourceInfo

_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._\- ]+")


@dataclass
class Payload:
    node_id: str
    file_name: str
    file_type: str
    data: bytes
    path: Path | None
    sha256: str
    source: SourceInfo
    depth: int = 0
    is_body: bool = False
    body_tables: list[list[list[str]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class UnpackResult:
    root: ContentNode
    payloads: list[Payload] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _HTMLCollector(HTMLParser):
    """提取可见文本与顶层表格；忽略 script/style。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._skip = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"}:
            self._skip += 1
        elif tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._table is not None:
            self._cell = []
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"}:
            self._skip = max(0, self._skip - 1)
        elif tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None and any(c for c in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0:
                if self._table:
                    self.tables.append(self._table)
                self._table = None
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = html.unescape(raw)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text_and_tables(markup: str) -> tuple[str, list[list[list[str]]]]:
    collector = _HTMLCollector()
    try:
        collector.feed(markup)
        collector.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup), []
    return collector.text(), collector.tables


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name or "").strip(" ._")
    return cleaned[:120] or fallback


def _header(msg: Message, key: str) -> str | None:
    value = msg.get(key)
    return str(value).strip() if value else None


def _iter_parts(message: Message):
    """遍历叶子 part；遇到 message/rfc822 作为叶子返回，交给递归解包控制深度。"""
    if message.is_multipart() or message.get_content_maintype() == "multipart":
        payload = message.get_payload()
        if isinstance(payload, list):
            for part in payload:
                if part.get_content_type() == "message/rfc822":
                    yield part
                else:
                    yield from _iter_parts(part)
        return
    yield message


def _source_from_message(msg: Message, file_type: str, digest: str, node_id: str) -> SourceInfo:
    return SourceInfo(
        file_name=(_header(msg, "Subject") or "email.eml")[:200],
        file_type=file_type,
        file_sha256=digest,
        email_subject=_header(msg, "Subject"),
        email_from=_header(msg, "From"),
        email_message_id=_header(msg, "Message-ID"),
        email_date=_header(msg, "Date"),
        content_node_id=node_id,
    )


class _Unpacker:
    def __init__(self, work_dir: Path, max_depth: int):
        self.work_dir = work_dir
        self.max_depth = max_depth
        self.counter = 0
        self.warnings: list[str] = []
        self.attach_dir = work_dir / "attachments"
        self.attach_dir.mkdir(parents=True, exist_ok=True)

    def _next_id(self, prefix: str = "n") -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    def unpack_eml(
        self, data: bytes, file_name: str, depth: int = 0, digest: str | None = None
    ) -> tuple[ContentNode, list[Payload]]:
        digest = digest or sha256_bytes(data)
        msg = email.message_from_bytes(data, policy=policy.default)
        node_id = self._next_id("email")
        node = ContentNode(
            node_id=node_id,
            kind="email",
            name=_header(msg, "Subject") or file_name,
            mime="message/rfc822",
            sha256=digest,
            size=len(data),
            depth=depth,
            provenance={
                "file_name": file_name,
                "from": _header(msg, "From"),
                "to": _header(msg, "To"),
                "cc": _header(msg, "Cc"),
                "date": _header(msg, "Date"),
                "message_id": _header(msg, "Message-ID"),
                "depth": depth,
            },
        )
        payloads: list[Payload] = []
        if depth >= self.max_depth:
            self.warnings.append(f"EML 递归深度达到上限 {self.max_depth}，停止下钻：{file_name}")
            return node, payloads

        plain_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[tuple[Message, str, bytes]] = []
        for part in _iter_parts(msg):
            if part.get_content_maintype() == "multipart" and part.get_content_type() != "message/rfc822":
                continue
            disposition = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            content_type = part.get_content_type()
            if content_type == "message/rfc822" and not filename:
                nested = part.get_payload()
                if isinstance(nested, list) and nested:
                    nested_bytes = nested[0].as_bytes()
                    attachments.append((nested[0], "nested.eml", nested_bytes))
                continue
            is_body_text = (
                part.get_content_maintype() == "text"
                and not filename
                and disposition != "attachment"
            )
            if is_body_text:
                try:
                    content = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    content = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                if content_type == "text/html":
                    html_parts.append(content)
                else:
                    plain_parts.append(content)
                continue
            if filename or disposition == "attachment" or content_type == "message/rfc822":
                payload_bytes = part.get_payload(decode=True) or b""
                if not payload_bytes and content_type == "message/rfc822":
                    nested = part.get_payload()
                    if isinstance(nested, list) and nested:
                        payload_bytes = nested[0].as_bytes()
                name = filename or ("nested.eml" if content_type == "message/rfc822" else "attachment.bin")
                attachments.append((part, name, payload_bytes))
            elif content_type.startswith("image/") and disposition == "inline":
                self.warnings.append(f"跳过内嵌签名图片：{content_type}")

        body_tables: list[list[list[str]]] = []
        body_text = "\n".join(part.strip() for part in plain_parts if part.strip())
        if html_parts:
            html_text, body_tables = html_to_text_and_tables("\n".join(html_parts))
            if not body_text:
                body_text = html_text
        if body_text:
            body_id = self._next_id("body")
            source = _source_from_message(msg, "EML正文", digest, body_id)
            body_path = self.attach_dir / _safe_filename(f"{body_id}-body.txt", "body.txt")
            body_path.write_text(body_text, encoding="utf-8")
            body_node = ContentNode(
                node_id=body_id,
                kind="body",
                name="邮件正文",
                mime="text/plain",
                sha256=sha256_bytes(body_text.encode("utf-8")),
                size=len(body_text),
                depth=depth + 1,
                provenance={"message_id": _header(msg, "Message-ID"), "parent": node_id},
            )
            node.children.append(body_node)
            payloads.append(
                Payload(
                    node_id=body_id,
                    file_name="邮件正文.txt",
                    file_type="eml_body",
                    data=body_text.encode("utf-8"),
                    path=body_path,
                    sha256=body_node.sha256 or "",
                    source=source,
                    depth=depth,
                    is_body=True,
                    body_tables=body_tables,
                )
            )

        for index, (part, name, payload_bytes) in enumerate(attachments):
            attach_id = self._next_id("att")
            file_type = detect_format_bytes(payload_bytes, name)
            source = _source_from_message(msg, f"EML附件-{file_type.upper()}", digest, attach_id)
            source.file_name = name
            path = self.attach_dir / _safe_filename(f"{attach_id}-{name}", f"{attach_id}.bin")
            path.write_bytes(payload_bytes)
            attach_node = ContentNode(
                node_id=attach_id,
                kind="attachment",
                name=name,
                mime=part.get_content_type() if isinstance(part, Message) else None,
                sha256=sha256_bytes(payload_bytes),
                size=len(payload_bytes),
                depth=depth + 1,
                provenance={"parent": node_id, "index": index},
            )
            node.children.append(attach_node)
            if file_type == "eml":
                nested_node, nested_payloads = self.unpack_eml(
                    payload_bytes, name, depth=depth + 1, digest=attach_node.sha256
                )
                attach_node.children.append(nested_node)
                payloads.extend(nested_payloads)
            else:
                payloads.append(
                    Payload(
                        node_id=attach_id,
                        file_name=name,
                        file_type=file_type,
                        data=payload_bytes,
                        path=path,
                        sha256=attach_node.sha256 or "",
                        source=source,
                        depth=depth + 1,
                    )
                )
        return node, payloads


def unpack_file(ingested: IngestedFile, work_dir: Path, max_depth: int = 3) -> UnpackResult:
    data = ingested.path.read_bytes()
    if ingested.detected_type == "eml":
        unpacker = _Unpacker(work_dir, max_depth)
        root, payloads = unpacker.unpack_eml(
            data, ingested.file_name, depth=0, digest=ingested.sha256
        )
        warnings = unpacker.warnings
        if not payloads:
            warnings.append("EML 未找到可解析的正文或附件")
        return UnpackResult(root=root, payloads=payloads, warnings=warnings)

    node_id = "file-0001"
    root = ContentNode(
        node_id=node_id,
        kind="file",
        name=ingested.file_name,
        mime=ingested.detected_type,
        sha256=ingested.sha256,
        size=ingested.size,
        provenance={"path": str(ingested.path)},
    )
    source = SourceInfo(
        file_name=ingested.file_name,
        file_type=ingested.detected_type.upper(),
        file_sha256=ingested.sha256,
        file_size=ingested.size,
        content_node_id=node_id,
    )
    payload = Payload(
        node_id=node_id,
        file_name=ingested.file_name,
        file_type=ingested.detected_type,
        data=data,
        path=ingested.path,
        sha256=ingested.sha256,
        source=source,
        warnings=list(ingested.warnings),
    )
    return UnpackResult(root=root, payloads=[payload], warnings=list(ingested.warnings))
