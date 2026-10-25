"""流水线状态对象（LangGraph State 的载荷）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from app.agent.nodes.arap import ArapDecision
from app.agent.nodes.match import MergedValues
from app.agent.nodes.unpack_eml import Payload
from app.extraction.schemas import (
    BillRecord,
    ContentNode,
    DocumentExtraction,
    MatchResult,
    NormalizedCharge,
    NormalizedDocument,
    Operation,
    UIRDocument,
)


@dataclass
class FileTask:
    path: Path
    file_name: str
    sha256: str
    detected_type: str
    status: str = "queued"
    message: str | None = None
    review_reason: str | None = None
    parse_modes: list[str] = field(default_factory=list)
    document_count: int = 0
    record_count: int = 0
    duplicate_of: str | None = None
    payloads: list[Payload] = field(default_factory=list)
    content_tree: ContentNode | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class UIRWork:
    file: FileTask
    uir: UIRDocument


@dataclass
class ExtractionWork:
    file: FileTask
    uir: UIRDocument
    extractions: list[DocumentExtraction] = field(default_factory=list)


@dataclass
class DocWork:
    file: FileTask
    uir: UIRDocument
    extraction: DocumentExtraction
    doc: NormalizedDocument
    arap: ArapDecision | None = None


@dataclass
class ChargeDecision:
    work: DocWork
    charge: NormalizedCharge
    arap: ArapDecision
    match: MatchResult
    operation: Operation
    merged: MergedValues
    operation_review_reason: str | None = None
    operation_notes: list[str] = field(default_factory=list)
    review_reason: str | None = None
    review_notes: list[str] = field(default_factory=list)
    low_confidence: bool = False
    confidence: float = 0.0
    posting_type: str | None = None


class PipelineState(TypedDict, total=False):
    batch_id: str
    input_paths: list[str]
    bill_paths: list[str]
    work_dir: str
    output_dir: str
    files: list[FileTask]
    uirs: list[UIRWork]
    extractions: list[ExtractionWork]
    documents: list[DocWork]
    decisions: list[ChargeDecision]
    records: list[BillRecord]
    prebuilt_records: list[BillRecord]
    warnings: list[str]
    skipped_inputs: list[str]
    error: str | None
    batch_output: Any
