"""AR/AP 决策矩阵（FR-13 / D1 v1.1 符号-类型正交 / WP-06）。

附录 A 修正规则：单据类型决定基础方向；金额负号只在 DN/Invoice 上反转一次；
Credit Note 的负号仅为打印惯例，方向判定取绝对值，绝不二次反转。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.governance.dictionaries import resolve_role


@dataclass
class ArapDecision:
    posting_type: str | None
    reasoning: list[str] = field(default_factory=list)
    review_reason: str | None = None
    amount_multiplier: float = 1.0


def _is_credit_note(document_type: str | None) -> bool:
    doc = (document_type or "").upper()
    return "CREDIT" in doc or doc.strip() in {"CN", "C/N"} or "贷记" in doc


def _direction_from_statement(statement: str | None, ar_keywords: list[str], ap_keywords: list[str]) -> str | None:
    if not statement:
        return None
    text = statement.lower()
    signals = set()
    if any(keyword.lower() in text for keyword in ar_keywords):
        signals.add("AR")
    if any(keyword.lower() in text for keyword in ap_keywords):
        signals.add("AP")
    if len(signals) == 1:
        return next(iter(signals))
    return None


def determine_posting_type(
    document_type: str | None,
    issuer: str | None,
    recipient: str | None,
    entity: str,
    aliases: list[str],
    amount: float,
    policy: str = "reversed_posting",
    direction_statement: str | None = None,
    ar_keywords: list[str] | None = None,
    ap_keywords: list[str] | None = None,
) -> ArapDecision:
    if policy not in {"reversed_posting", "same_account_offset"}:
        raise ValueError(f"未知 AR/AP 口径：{policy}")

    reasoning = [f"识别签发方为 {issuer}，接收方为 {recipient}"]
    is_issuer = resolve_role(issuer, entity, aliases)
    is_recipient = resolve_role(recipient, entity, aliases)

    if is_issuer and is_recipient:
        reasoning.append("记账主体同时对应签发方与接收方，无法定位交易方向（SELF_TRADE）")
        return ArapDecision(None, reasoning, "SELF_TRADE")
    if not is_issuer and not is_recipient:
        reasoning.append(
            f"记账主体 {entity} 与签发方/接收方均无法对应（第三方代收代付或别名缺失，ENTITY_UNRESOLVED）"
        )
        return ArapDecision(None, reasoning, "ENTITY_UNRESOLVED")

    role = "签发方" if is_issuer else "接收方"
    credit_note = _is_credit_note(document_type)

    if policy == "reversed_posting":
        if credit_note:
            posting = "AP" if is_issuer else "AR"
        else:
            posting = "AR" if is_issuer else "AP"
        reasoning.append(
            f"单据类型为 {document_type}，记账主体为{role}，基础矩阵判定为 {posting}"
        )
    else:  # same_account_offset：CN 不翻转科目，由装配层将金额取负
        posting = "AR" if is_issuer else "AP"
        reasoning.append(
            f"红冲口径 same_account_offset：科目不翻转，判定为 {posting}"
        )

    amount_multiplier = 1.0
    if credit_note:
        reasoning.append(
            f"Credit Note 金额 {amount} 的负号仅为冲减表示形式，方向判定取绝对值，不二次反转"
        )
        if policy == "same_account_offset" and amount > 0:
            amount_multiplier = -1.0
            reasoning.append("红冲口径要求金额取负，装配层将输出金额乘以 -1")
    elif amount < 0:
        posting = "AP" if posting == "AR" else "AR"
        reasoning.append(f"常规单据金额为负数 {amount}，视为冲红，方向反转为 {posting}")

    review_reason = None
    statement_signal = _direction_from_statement(
        direction_statement, ar_keywords or [], ap_keywords or []
    )
    if statement_signal:
        reasoning.append(
            f"正文收费方向语句“{direction_statement}”佐证为 {statement_signal}"
        )
        if statement_signal != posting:
            review_reason = "OTHER"
            reasoning.append(
                f"方向语句与矩阵结论 {posting} 矛盾，按 fail-safe 送人工复核"
            )

    reasoning.append(f"结论：posting_type = {posting}")
    return ArapDecision(posting, reasoning, review_reason, amount_multiplier)


def document_amount(charges_amounts: list[float | None], totals: list[float] | None = None) -> float:
    total = sum(amount or 0.0 for amount in charges_amounts)
    if abs(total) < 1e-9 and totals:
        total = sum(totals)
    return round(total, 2)
