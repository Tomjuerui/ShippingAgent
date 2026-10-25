"""记录展开与输出装配（FR-10/17/18，D11 / WP-08）。"""

from __future__ import annotations

from collections import defaultdict

from app.agent.state import ChargeDecision, FileTask
from app.config.settings import Settings
from app.extraction.schemas import (
    BillRecord,
    ChangeInfoOut,
    FinancialAttributesOut,
    MatchInfoOut,
    MatchLevel,
    Operation,
    AuditTrailOut,
    SourceOut,
    ValidationOut,
)
from app.governance.dictionaries import normalize_key


def _fmt(value) -> str:
    if value is None or value == "":
        return "null"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _mapping_sentence(decision: ChargeDecision) -> str:
    charge = decision.charge
    original = charge.original_charge_name
    standard = charge.charge_item
    if not original:
        return "费用名称缺失，无法映射标准费用项"
    if not standard:
        return f"原始费用名称 '{original}' 未收录于标准字典，保留原文"
    if normalize_key(original) == normalize_key(standard):
        return f"费用名称已是标准项 '{standard}'"
    suffix = ""
    if charge.standardized_by == "fuzzy":
        suffix = "（模糊匹配归类）"
    elif charge.standardized_by in {"embedding", "llm"}:
        suffix = "（语义归类）"
    return f"原始费用名称 '{original}' 已映射为标准费用项 '{standard}'{suffix}"


def _source_line(decision: ChargeDecision) -> str:
    source = decision.work.doc.source
    parts = [
        f"来源文件：{source.file_name}（{source.file_type or 'unknown'}）",
        f"SHA-256 {source.file_sha256[:12]}…" if source.file_sha256 else "",
    ]
    if source.email_subject:
        parts.append(f"邮件主题：{source.email_subject}")
    if source.email_message_id:
        parts.append(f"Message-ID：{source.email_message_id}")
    return "；".join(part for part in parts if part)


def _match_lines(decision: ChargeDecision) -> list[str]:
    match = decision.match
    if match.ambiguous or (match.level != MatchLevel.NONE and match.bill is None):
        return [
            f"匹配到 {len(match.candidates)} 条候选账单，无法唯一命中，转人工复核（MATCH_AMBIGUOUS）：{match.evidence}"
        ]
    if match.bill is not None:
        return [
            f"通过 {match.level.value} 匹配到已有账单 {match.bill.bill_id}：{match.evidence}"
        ]
    return ["L1–L4 均未匹配到已有账单，判定为新增（CREATE）"]


def _change_lines(decision: ChargeDecision) -> list[str]:
    lines: list[str] = []
    change_info = decision.merged.change_info
    if decision.operation == Operation.UPDATE and change_info:
        for change in change_info.changed_fields[:10]:
            lines.append(
                f"根据输入材料将 {change.field} 由 {_fmt(change.old_value)} 更新为 {_fmt(change.new_value)}"
            )
        remaining = len(change_info.changed_fields) - 10
        if remaining > 0:
            lines.append(f"另有 {remaining} 个字段发生变更，详见 change_info.changed_fields")
        if change_info.inherited_fields:
            lines.append(
                f"其他 {len(change_info.inherited_fields)} 个字段未被明确要求修改，因此保留原值"
            )
    elif decision.operation == Operation.NOCHANGE:
        lines.append("输入与已有账单所有字段一致，无需更新（NOCHANGE）")
    return lines


def _validation_lines(decision: ChargeDecision) -> list[str]:
    lines: list[str] = []
    for issue in decision.work.doc.validation.issues:
        if issue.action == "review":
            continue
        if issue.severity in {"warning", "error"} or issue.action == "repaired":
            lines.append(f"校验/修复：[{issue.rule}] {issue.message}")
        if len(lines) >= 6:
            break
    return lines


def _review_lines(decision: ChargeDecision) -> list[str]:
    if not decision.review_reason:
        return []
    lines = [f"送人工复核（原因：{decision.review_reason}）"]
    lines.extend(decision.review_notes[:6])
    return lines


def _build_record(
    decision: ChargeDecision, record_id: str, settings: Settings
) -> BillRecord:
    self_name = settings.accounting_entity.name
    values = decision.merged
    amount = values.charge_details.amount
    multiplier = decision.arap.amount_multiplier
    extra_reasoning: list[str] = []
    if multiplier != 1.0 and amount is not None:
        new_amount = round(amount * multiplier, 2)
        values.charge_details.amount = new_amount
        if values.change_info:
            for change in values.change_info.changed_fields:
                if change.field == "charge_details.amount":
                    change.new_value = new_amount
        extra_reasoning.append(
            f"红冲口径 {settings.arap.policy}：输出金额由 {_fmt(amount)} 取负为 {_fmt(new_amount)}"
        )

    change_info = None
    if decision.operation == Operation.UPDATE or (
        decision.operation == Operation.PENDING_REVIEW
        and values.change_info
        and values.change_info.changed_fields
    ):
        change_info = ChangeInfoOut(
            changed_fields=values.change_info.changed_fields,
            inherited_fields=values.change_info.inherited_fields,
        )

    match_info = MatchInfoOut(
        matched_bill_id=decision.match.bill.bill_id if decision.match.bill else None,
        match_level=decision.match.level.value,
        match_evidence=decision.match.evidence or None,
        candidates=[
            candidate.bill_id for candidate in decision.match.candidates if candidate.bill_id
        ],
        derived_keys_used=decision.match.derived_keys_used,
    )

    reasoning: list[str] = [_source_line(decision), _mapping_sentence(decision)]
    reasoning.extend(decision.arap.reasoning)
    reasoning.extend(_match_lines(decision))
    reasoning.extend(decision.operation_notes)
    reasoning.extend(_change_lines(decision))
    reasoning.extend(_validation_lines(decision))
    reasoning.extend(extra_reasoning)
    reasoning.extend(_review_lines(decision))
    reasoning.append(
        f"记录置信度 {decision.confidence}，low_confidence={str(decision.low_confidence).lower()}"
    )
    deduped: list[str] = []
    seen_reasoning: set[str] = set()
    for line in reasoning:
        if line and line not in seen_reasoning:
            seen_reasoning.add(line)
            deduped.append(line)

    snippet = decision.charge.snippet
    if not snippet:
        snippet = " ".join(
            filter(
                None,
                [
                    decision.charge.original_charge_name,
                    _fmt(decision.charge.amount) if decision.charge.amount is not None else None,
                    decision.charge.currency,
                ],
            )
        )

    return BillRecord(
        record_id=record_id,
        operation=decision.operation,
        document_info=values.document_info,
        shipment_info=values.shipment_info,
        transaction_party=values.transaction_party,
        charge_details=values.charge_details,
        financial_attributes=FinancialAttributesOut(
            accounting_entity=self_name, posting_type=decision.posting_type
        ),
        match_info=match_info,
        change_info=change_info,
        validation=ValidationOut(
            passed=decision.work.doc.validation.passed,
            issues=decision.work.doc.validation.issues,
        ),
        confidence=decision.confidence,
        low_confidence=decision.low_confidence,
        review_reason=decision.review_reason,
        source=SourceOut(
            file_name=decision.work.doc.source.file_name,
            file_type=decision.work.doc.source.file_type,
            file_sha256=decision.work.doc.source.file_sha256,
            email_subject=decision.work.doc.source.email_subject,
            email_from=decision.work.doc.source.email_from,
            email_message_id=decision.work.doc.source.email_message_id,
            email_date=decision.work.doc.source.email_date,
            content_node_id=decision.work.doc.source.content_node_id,
        ),
        audit_trail=AuditTrailOut(
            original_text_snippet=snippet,
            ai_reasoning=deduped,
        ),
    )


def assemble_records(
    files: list[FileTask],
    decisions: list[ChargeDecision],
    prebuilt_by_sha: dict[str, list[BillRecord]],
    settings: Settings,
) -> list[BillRecord]:
    by_sha: dict[str, list[ChargeDecision]] = defaultdict(list)
    for decision in decisions:
        by_sha[decision.work.file.sha256].append(decision)

    records: list[BillRecord] = []
    for file_task in files:
        sha = file_task.sha256
        if sha in prebuilt_by_sha:
            clones = prebuilt_by_sha[sha]
            records.extend(clones)
            file_task.record_count = len(clones)
            continue
        if file_task.status == "skipped":
            continue
        if file_task.status == "failed" and sha not in by_sha:
            continue
        counter = 0
        for decision in by_sha.get(sha, []):
            counter += 1
            prefix = (sha or "unknown")[:8]
            record_id = f"{prefix}-{counter:03d}"
            records.append(_build_record(decision, record_id, settings))
        file_task.record_count = counter
    return records
