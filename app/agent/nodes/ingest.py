"""文件接入与真实格式探测（FR-01）。"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_INPUT_TYPES = {
    "eml",
    "pdf",
    "png",
    "jpeg",
    "tiff",
    "xlsx",
    "xls",
    "docx",
    "doc",
    "text",
}

_EML_HEADER = re.compile(
    r"^(from|to|cc|subject|date|message-id|mime-version|received|return-path):",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class IngestedFile:
    path: Path
    file_name: str
    sha256: str
    size: int
    detected_type: str
    extension_type: str
    warnings: list[str] = field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        return self.detected_type in SUPPORTED_INPUT_TYPES


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extension_type(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".")
    return {
        "jpg": "jpeg",
        "tif": "tiff",
        "txt": "text",
        "text": "text",
        "htm": "text",
        "html": "text",
    }.get(ext, ext or "unknown")


def _looks_like_eml(head: bytes) -> bool:
    try:
        text = head.decode("utf-8", errors="ignore")
    except Exception:  # pragma: no cover
        return False
    return len(_EML_HEADER.findall(text)) >= 2


def _zip_type(data: bytes) -> str:
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except Exception:
        return "zip"
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    return "zip"


def detect_format_bytes(data: bytes, file_name: str = "") -> str:
    ext = _extension_type(file_name)
    head = data[:8192]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "tiff"
    if head.startswith(b"PK\x03\x04"):
        return _zip_type(data)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # OLE 复合文档：扩展名区分 Word / Excel
        return ext if ext in {"doc", "xls"} else "ole"
    if _looks_like_eml(head):
        return "eml"
    if ext in SUPPORTED_INPUT_TYPES:
        # 文本类扩展名且内容可按文本解码
        if ext in {"text"}:
            try:
                data[:4096].decode("utf-8")
                return "text"
            except UnicodeDecodeError:
                return "unknown"
        return ext
    return "unknown"


def detect_format(path: Path) -> str:
    with path.open("rb") as fh:
        head = fh.read(8192)
        if head.startswith(b"PK\x03\x04"):
            fh.seek(0)
            return _zip_type(fh.read())
    return detect_format_bytes(head, path.name)


def ingest_file(path: Path) -> IngestedFile:
    path = path.resolve()
    size = path.stat().st_size
    digest = sha256_file(path)
    detected = detect_format(path)
    extension = _extension_type(path.name)
    warnings: list[str] = []
    if extension in SUPPORTED_INPUT_TYPES and detected != extension:
        warnings.append(f"扩展名 {extension} 与真实格式 {detected} 不一致，已按魔数路由")
    return IngestedFile(
        path=path,
        file_name=path.name,
        sha256=digest,
        size=size,
        detected_type=detected,
        extension_type=extension,
        warnings=warnings,
    )


def discover_inputs(inputs: list[str | Path]) -> tuple[list[IngestedFile], list[str]]:
    """展开目录/文件列表；返回 (可处理文件, 跳过说明)。"""
    paths: list[Path] = []
    messages: list[str] = []
    for item in inputs:
        path = Path(item).expanduser()
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not child.name.startswith("."):
                    paths.append(child)
        elif path.is_file():
            paths.append(path)
        else:
            messages.append(f"输入不存在：{item}")
    ingested: list[IngestedFile] = []
    for path in paths:
        if path.suffix.lower() == ".json":
            messages.append(f"跳过 JSON（应作为已有账单通过 --bills 传入）：{path.name}")
            continue
        try:
            item = ingest_file(path)
        except Exception as exc:  # 坏文件隔离
            messages.append(f"读取失败 {path.name}: {exc}")
            continue
        if not item.is_supported:
            messages.append(f"不支持的格式 {item.detected_type}：{path.name}")
            continue
        ingested.append(item)
    return ingested, messages
