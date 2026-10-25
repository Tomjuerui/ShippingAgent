"""全链路数据契约（实现计划 WP-01，冻结版 v1.1）。

变更流程：任何字段增删改需登记契约版本号 → 同步《实现计划.md》→ 通知编排者。
子智能体不得私自修改本文件（全局禁止事项）。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class Operation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NOCHANGE = "NOCHANGE"
    PENDING_REVIEW = "PENDING_REVIEW"


class PostingType(str, Enum):
    AR = "AR"
    AP = "AP"


class MatchLevel(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    NONE = "NONE"


class ReviewReason(str, Enum):
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"
    SUM_MISMATCH = "SUM_MISMATCH"
    MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"
    DATA_CONFLICT = "DATA_CONFLICT"
    ENTITY_UNRESOLVED = "ENTITY_UNRESOLVED"
    SELF_TRADE = "SELF_TRADE"
    SCHEMA_RETRY_EXCEEDED = "SCHEMA_RETRY_EXCEEDED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OTHER = "OTHER"


# --------------------------------------------------------------------------- #
# 数值清洗（LLM / OCR 输出宽容解析；只做格式归一，不做业务判断）
# --------------------------------------------------------------------------- #
_NUMBER_STRIP = re.compile(r"[^\d.\-+]")


def coerce_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "null", "none", "-", "--", "nan"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(" ", "").replace(",", "")
    # 仅在含数字的串上做 OCR 混淆修复，避免误伤 "N/A" 等文本
    if any(ch.isdigit() for ch in text):
        text = text.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    text = _NUMBER_STRIP.sub("", text)
    if text.count(".") > 1:
        head, tail = text.rsplit(".", 1)
        text = head.replace(".", "") + "." + tail
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return -number if negative else number


def _clamp_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


# --------------------------------------------------------------------------- #
# 来源 / 内容树 / UIR
# --------------------------------------------------------------------------- #
class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_name: str
    file_type: str = "unknown"
    file_sha256: str = ""
    file_size: int | None = None
    email_subject: str | None = None
    email_from: str | None = None
    email_message_id: str | None = None
    email_date: str | None = None
    content_node_id: str | None = None


class ContentNode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    node_id: str
    kind: Literal["email", "body", "attachment", "document_chunk", "file"]
    name: str
    mime: str | None = None
    sha256: str | None = None
    size: int | None = None
    depth: int = 0
    provenance: dict[str, Any] = Field(default_factory=dict)
    children: list["ContentNode"] = Field(default_factory=list)


class UIRTable(BaseModel):
    model_config = ConfigDict(extra="ignore")
    table_id: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    locator: dict[str, Any] = Field(default_factory=dict)


class UIRBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    block_id: str
    kind: Literal["text", "table"] = "text"
    text: str | None = None
    table: UIRTable | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None


class UIRDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uir_id: str
    source: SourceInfo
    content_node_id: str | None = None
    blocks: list[UIRBlock] = Field(default_factory=list)
    parse_mode: str = "text"  # text | ocr | excel | word | eml_body
    parse_quality: str = "high"  # high | low
    warnings: list[str] = Field(default_factory=list)

    def full_text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.kind == "text" and block.text:
                parts.append(block.text)
            elif block.kind == "table" and block.table:
                table = block.table
                if table.headers:
                    parts.append(" | ".join(table.headers))
                parts.extend(" | ".join(row) for row in table.rows)
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# LLM 提取契约（D6：只含客观事实，不含 posting_type / operation / 标准名 / ISO 码）
# --------------------------------------------------------------------------- #
class ChargeLine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    line_id: str | None = None
    original_charge_name: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None
    snippet: str | None = None
    locator: dict[str, Any] | None = None
    confidence: dict[str, float] = Field(default_factory=dict)

    _coerce = field_validator("quantity", "unit_price", "amount", mode="before")(coerce_number)

    @field_validator("confidence")
    @classmethod
    def _conf(cls, value: dict[str, float]) -> dict[str, float]:
        return {k: _clamp_confidence(v) for k, v in value.items() if v is not None}


class ShipmentInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bl_no: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    vessel_voyage: str | None = None
    pol: str | None = None
    pod: str | None = None


class TotalEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["subtotal", "tax", "total"] = "total"
    amount: float | None = None
    currency: str | None = None

    _coerce = field_validator("amount", mode="before")(coerce_number)


class DocumentExtraction(BaseModel):
    """LLM 单次调用的输出契约（D6）。"""

    model_config = ConfigDict(extra="ignore")
    extraction_id: str | None = None
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    shipment_info: ShipmentInfo = Field(default_factory=ShipmentInfo)
    issuer: str | None = None
    recipient: str | None = None
    charges: list[ChargeLine] = Field(default_factory=list)
    totals: list[TotalEntry] = Field(default_factory=list)
    direction_statement: str | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    source_chunk_id: str | None = None

    @field_validator("field_confidence")
    @classmethod
    def _conf(cls, value: dict[str, float]) -> dict[str, float]:
        return {k: _clamp_confidence(v) for k, v in value.items() if v is not None}


class ExtractionChunk(BaseModel):
    """segment 节点产物：一份 UIR 可切成多个待提取块。"""

    model_config = ConfigDict(extra="ignore")
    chunk_id: str
    uir_id: str
    text: str
    source: SourceInfo
    content_node_id: str | None = None
    page_locator: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 校验 / 规范化中间态
# --------------------------------------------------------------------------- #
class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rule: str
    message: str
    action: str = "none"  # none | repaired | review
    field: str | None = None
    severity: str = "warning"  # info | warning | error


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    passed: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        if issue.severity == "error" or issue.action == "review":
            self.passed = False


class NormalizedCharge(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_line_id: str | None = None
    original_charge_name: str | None = None
    charge_item: str | None = None
    standardized_by: str | None = None  # exact | normalized | alias | fuzzy | none
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None
    snippet: str | None = None
    locator: dict[str, Any] | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.8


class NormalizedDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uir_id: str
    source: SourceInfo
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    shipment_info: ShipmentInfo = Field(default_factory=ShipmentInfo)
    issuer: str | None = None
    recipient: str | None = None
    direction_statement: str | None = None
    charges: list[NormalizedCharge] = Field(default_factory=list)
    totals: list[TotalEntry] = Field(default_factory=list)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.8
    validation: ValidationResult = Field(default_factory=ValidationResult)
    raw: DocumentExtraction | None = None


# --------------------------------------------------------------------------- #
# 已有账单（输入，双 Schema）与匹配
# --------------------------------------------------------------------------- #
class DocumentInfoOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None


class ShipmentInfoOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bl_no: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    vessel_voyage: str | None = None
    pol: str | None = None
    pod: str | None = None


class TransactionPartyOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    issuer: str | None = None
    recipient: str | None = None


class ChargeDetailsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    original_charge_name: str | None = None
    charge_item: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None

    _coerce = field_validator("quantity", "unit_price", "amount", mode="before")(coerce_number)


class FinancialAttributesOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    accounting_entity: str
    posting_type: str | None = None


class ExistingBill(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bill_id: str
    version: int = 1
    document_info: DocumentInfoOut = Field(default_factory=DocumentInfoOut)
    shipment_info: ShipmentInfoOut = Field(default_factory=ShipmentInfoOut)
    transaction_party: TransactionPartyOut = Field(default_factory=TransactionPartyOut)
    charge: ChargeDetailsOut = Field(default_factory=ChargeDetailsOut)
    financial_attributes: FinancialAttributesOut | None = None
    updated_at: str | None = None
    source_format: str = "extended"  # extended | minimal
    derived_keys: dict[str, bool] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class MatchInfoOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    matched_bill_id: str | None = None
    match_level: str = MatchLevel.NONE.value
    match_evidence: str | None = None
    candidates: list[str] = Field(default_factory=list)
    derived_keys_used: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    level: MatchLevel = MatchLevel.NONE
    bill: ExistingBill | None = None
    candidates: list[ExistingBill] = Field(default_factory=list)
    evidence: str = ""
    ambiguous: bool = False
    derived_keys_used: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 输出契约（PRD 6.3 / 6.5）
# --------------------------------------------------------------------------- #
class ChangedField(BaseModel):
    model_config = ConfigDict(extra="ignore")
    field: str
    old_value: Any = None
    new_value: Any = None
    inherited: bool = False


class InheritedField(BaseModel):
    model_config = ConfigDict(extra="ignore")
    field: str
    value: Any = None


class ChangeInfoOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    changed_fields: list[ChangedField] = Field(default_factory=list)
    inherited_fields: list[InheritedField] = Field(default_factory=list)


class ValidationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    passed: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)


class SourceOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_name: str | None = None
    file_type: str | None = None
    file_sha256: str | None = None
    email_subject: str | None = None
    email_from: str | None = None
    email_message_id: str | None = None
    email_date: str | None = None
    content_node_id: str | None = None


class AuditTrailOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    original_text_snippet: str | None = None
    ai_reasoning: list[str] = Field(default_factory=list)


class BillRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    record_id: str
    operation: Operation
    document_info: DocumentInfoOut = Field(default_factory=DocumentInfoOut)
    shipment_info: ShipmentInfoOut = Field(default_factory=ShipmentInfoOut)
    transaction_party: TransactionPartyOut = Field(default_factory=TransactionPartyOut)
    charge_details: ChargeDetailsOut = Field(default_factory=ChargeDetailsOut)
    financial_attributes: FinancialAttributesOut
    match_info: MatchInfoOut = Field(default_factory=MatchInfoOut)
    change_info: ChangeInfoOut | None = None
    validation: ValidationOut = Field(default_factory=ValidationOut)
    confidence: float = 0.0
    low_confidence: bool = False
    review_reason: str | None = None
    source: SourceOut = Field(default_factory=SourceOut)
    audit_trail: AuditTrailOut = Field(default_factory=AuditTrailOut)


class OperationCounts(BaseModel):
    model_config = ConfigDict(extra="ignore")
    CREATE: int = 0
    UPDATE: int = 0
    NOCHANGE: int = 0
    PENDING_REVIEW: int = 0


class MatchKeyCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    extended: float = 0.0
    minimal: float = 0.0
    L1_ready: float = 0.0
    L2_ready: float = 0.0
    L3_ready: float = 0.0
    L4_ready: float = 0.0


class FileResultOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_name: str
    file_sha256: str | None = None
    status: str = "completed"  # completed | failed | skipped
    parse_mode: str | None = None
    document_count: int = 0
    record_count: int = 0
    review_reason: str | None = None
    message: str | None = None
    duplicate_of: str | None = None


class BatchSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    records: int = 0
    operation_counts: OperationCounts = Field(default_factory=OperationCounts)
    review_reasons: dict[str, int] = Field(default_factory=dict)
    match_key_coverage: MatchKeyCoverage = Field(default_factory=MatchKeyCoverage)


class BatchMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pipeline_version: str = "1.0.0"
    prompt_version: str = "v1"
    dictionary_version: str = ""
    llm_profile: str | None = None
    llm_model: str | None = None
    arap_policy: str = "reversed_posting"
    ocr_exposer: str | None = None
    extraction_mode: str | None = None


class BatchOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    batch_id: str
    processed_at: str
    summary: BatchSummary = Field(default_factory=BatchSummary)
    files: list[FileResultOut] = Field(default_factory=list)
    records: list[BillRecord] = Field(default_factory=list)
    meta: BatchMeta = Field(default_factory=BatchMeta)


class FileProgress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_name: str
    file_sha256: str | None = None
    status: str = "queued"  # queued | ocr | extracting | deciding | completed | failed | skipped
    stage: str | None = None
    record_count: int = 0
    review_reason: str | None = None
    message: str | None = None


class BatchStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    batch_id: str
    status: str = "queued"  # queued | running | completed | failed
    progress: int = 0
    total_files: int = 0
    processed_files: int = 0
    files: list[FileProgress] = Field(default_factory=list)
    operation_counts: OperationCounts = Field(default_factory=OperationCounts)
    created_at: str = ""
    finished_at: str | None = None
    error: str | None = None


ContentNode.model_rebuild()
