"""复核路由与置信度门槛（FR-16 / WP-08）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.nodes.arap import ArapDecision
from app.config.settings import Settings
from app.extraction.schemas import NormalizedCharge, NormalizedDocument, Operation
from app.agent.nodes.validate import review_reason_from_validation

_POSTING_NULL_REASONS = {"ENTITY_UNRESOLVED", "SELF_TRADE"}


@dataclass
class ReviewOutcome:
    operation: Operation
    review_reason: str | None
    low_confidence: bool
    confidence: float
    posting_type: str | None
    notes: list[str] = field(default_factory=list)


def finalize_review(
    operation: Operation,
    operation_review_reason: str | None,
    doc: NormalizedDocument,
    charge: NormalizedCharge,
    arap: ArapDecision,
    settings: Settings,
) -> ReviewOutcome:
    confidence = round(min(doc.confidence, charge.confidence), 3)
    low_confidence = confidence < settings.thresholds.low_confidence_below
    validation_reason = review_reason_from_validation(doc.validation)
    notes: list[str] = []

    if arap.review_reason:
        review_reason = arap.review_reason
        notes.append(f"AR/AP 复核触发：{arap.review_reason}")
    elif operation_review_reason:
        review_reason = operation_review_reason
        notes.append(f"匹配/更新复核触发：{operation_review_reason}")
    elif validation_reason:
        review_reason = validation_reason
        notes.append(f"数据校验未通过（{validation_reason}）")
    elif confidence < settings.thresholds.review_below:
        review_reason = "OTHER"
        notes.append(
            f"记录置信度 {confidence} 低于复核阈值 {settings.thresholds.review_below}"
        )
    else:
        review_reason = None

    if review_reason:
        operation = Operation.PENDING_REVIEW
    elif operation == Operation.PENDING_REVIEW:
        review_reason = "OTHER"
        notes.append("操作被标记为待复核但未给出原因，按 OTHER 记录")

    review_issues = [
        issue.message
        for issue in doc.validation.issues
        if issue.action == "review" or issue.severity == "error"
    ][:5]
    notes.extend(f"校验：{message}" for message in review_issues)
    if low_confidence:
        notes.append(
            f"low_confidence=true（置信度 {confidence} < {settings.thresholds.low_confidence_below}）"
        )

    posting = arap.posting_type
    if review_reason in _POSTING_NULL_REASONS:
        posting = None

    return ReviewOutcome(
        operation=operation,
        review_reason=review_reason,
        low_confidence=low_confidence,
        confidence=confidence,
        posting_type=posting,
        notes=notes,
    )
