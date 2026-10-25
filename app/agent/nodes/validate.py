"""数据校验引擎（FR-14 V1~V7 / WP-05）。校验失败 → 修复留痕或送复核。"""

from __future__ import annotations

import re
from collections import defaultdict

from app.config.settings import Settings
from app.extraction.schemas import (
    NormalizedCharge,
    NormalizedDocument,
    ValidationIssue,
    ValidationResult,
)

_SUMMARY_NAME = re.compile(
    r"^(sub\s*-?\s*total|subtotal|total|grand\s+total|amount\s+due|vat|tax|gst|"
    r"小计|合计|总计|税额|增值税|税费|费用合计)$",
    re.IGNORECASE,
)

RULE_TO_REASON = {
    "V1": "OTHER",
    "V2": "OCR_LOW_QUALITY",
    "V3": "SUM_MISMATCH",
    "V4": "OCR_LOW_QUALITY",
    "V5": "OTHER",
    "V6": "OTHER",
    "V7": "SUM_MISMATCH",
    "V8": "OTHER",
    "V9": "SUM_MISMATCH",
    "NO_CHARGES": "OTHER",
}


def _is_summary_row(charge: NormalizedCharge) -> bool:
    name = (charge.original_charge_name or "").strip()
    if not name:
        return False
    return bool(_SUMMARY_NAME.match(name))


def _currency_key(value: str | None) -> str:
    return (value or "").upper()


def validate_document(doc: NormalizedDocument, settings: Settings) -> ValidationResult:
    result = doc.validation
    tolerance = settings.matching.amount_tolerance

    # 汇总行过滤 + 单据内重复提取
    filtered: list[NormalizedCharge] = []
    seen: set[tuple] = set()
    for charge in doc.charges:
        if _is_summary_row(charge):
            result.add(
                ValidationIssue(
                    rule="V5",
                    message=f"合计行不作为费用明细：{charge.original_charge_name}",
                    action="none",
                    field="original_charge_name",
                    severity="info",
                )
            )
            continue
        key = (
            re.sub(r"\s+", "", (charge.original_charge_name or "").lower()),
            None if charge.amount is None else round(charge.amount, 2),
            _currency_key(charge.currency),
        )
        if key in seen:
            result.add(
                ValidationIssue(
                    rule="V5",
                    message=f"同一费用重复提取，已去重：{charge.original_charge_name}",
                    action="none",
                    field="original_charge_name",
                    severity="warning",
                )
            )
            continue
        seen.add(key)
        filtered.append(charge)
    doc.charges = filtered

    for index, charge in enumerate(doc.charges):
        prefix = f"charges[{index}]"
        if charge.amount is None:
            result.add(
                ValidationIssue(
                    rule="V1",
                    message=f"{prefix} 金额缺失或无法解析",
                    action="review",
                    field=f"{prefix}.amount",
                    severity="error",
                )
            )
        else:
            rounded = round(charge.amount, 2)
            if abs(rounded - charge.amount) > 1e-9:
                original_amount = charge.amount
                charge.amount = rounded
                result.add(
                    ValidationIssue(
                        rule="V1",
                        message=(
                            f"{prefix} 金额小数位超过 2 位，已四舍五入："
                            f"{original_amount} → {rounded}"
                        ),
                        action="none",
                        field=f"{prefix}.amount",
                        severity="info",
                    )
                )
            if (
                charge.quantity is not None
                and charge.unit_price is not None
                and abs(abs(charge.quantity * charge.unit_price) - abs(charge.amount)) > tolerance
            ):
                expected = round(charge.quantity * charge.unit_price, 2)
                result.add(
                    ValidationIssue(
                        rule="V7",
                        message=(
                            f"{prefix} 数量×单价≠金额：{charge.quantity} × {charge.unit_price} "
                            f"= {expected}，金额为 {charge.amount}"
                        ),
                        action="review",
                        field="amount",
                        severity="error",
                    )
                )
        if not charge.currency:
            result.add(
                ValidationIssue(
                    rule="V2",
                    message=f"{prefix} 币种为空，无法完成币种校验与 L2/L3 匹配",
                    action="none",
                    field=f"{prefix}.currency",
                    severity="warning",
                )
            )

    # V3 分币种勾稽
    grouped: dict[str, list[float]] = defaultdict(list)
    for charge in doc.charges:
        grouped[_currency_key(charge.currency)].append(charge.amount or 0.0)
    subtotals: dict[str, float] = {}
    taxes: dict[str, float] = {}
    totals: dict[str, float] = {}
    for total in doc.totals:
        if total.amount is None:
            continue
        key = _currency_key(total.currency)
        if total.kind == "subtotal":
            subtotals[key] = total.amount
        elif total.kind == "tax":
            taxes[key] = total.amount
        else:
            totals[key] = total.amount

    single_currency = next(iter(grouped)) if len(grouped) == 1 else None
    only_subtotal = next(iter(subtotals.values())) if len(subtotals) == 1 else None
    only_tax = next(iter(taxes.values())) if len(taxes) == 1 else None
    only_total = next(iter(totals.values())) if len(totals) == 1 else None
    for currency, amounts in grouped.items():
        subtotal = subtotals.get(currency)
        total = totals.get(currency)
        tax = taxes.get(currency)
        if single_currency == currency:
            subtotal = subtotal if subtotal is not None else only_subtotal
            total = total if total is not None else only_total
            tax = tax if tax is not None else only_tax

        line_sum = round(sum(amounts), 2)
        if subtotal is not None and abs(line_sum - subtotal) > tolerance:
            result.add(
                ValidationIssue(
                    rule="V3",
                    message=(
                        f"分币种明细合计 {line_sum} {currency or '(无币种)'} "
                        f"≠ 小计 {subtotal}（差额 {round(line_sum - subtotal, 2)}）"
                    ),
                    action="review",
                    field="totals",
                    severity="error",
                )
            )
        if subtotal is not None and total is not None:
            expected = round(subtotal + (tax or 0.0), 2)
            if abs(expected - total) > tolerance:
                result.add(
                    ValidationIssue(
                        rule="V3",
                        message=(
                            f"小计+税额 {expected} ≠ 总计 {total} "
                            f"（币种 {currency or '未知'}）"
                        ),
                        action="review",
                        field="totals",
                        severity="error",
                    )
                )
        elif subtotal is None and total is not None:
            expected = round(line_sum + (tax or 0.0), 2)
            if abs(expected - total) > tolerance:
                result.add(
                    ValidationIssue(
                        rule="V3",
                        message=(
                            f"明细合计+税额 {expected} ≠ 总计 {total} "
                            f"（币种 {currency or '未知'}）"
                        ),
                        action="review",
                        field="totals",
                        severity="error",
                    )
                )
        if total is not None and total != 0:
            for charge in doc.charges:
                if _currency_key(charge.currency) != currency:
                    continue
                if charge.amount is not None and abs(charge.amount) > abs(total) + tolerance:
                    result.add(
                        ValidationIssue(
                            rule="V9",
                            message=(
                                f"单项金额 {charge.amount} 大于总计 {total}，疑似串行/串币种"
                            ),
                            action="review",
                            field="amount",
                            severity="error",
                        )
                    )
                    break

    if not doc.charges:
        result.add(
            ValidationIssue(
                rule="NO_CHARGES",
                message="未提取到任何有效费用行",
                action="none",
                field="charges",
                severity="info",
            )
        )

    result.passed = not any(
        issue.action == "review" or issue.severity == "error" for issue in result.issues
    )
    if doc.charges:
        doc.confidence = min(doc.confidence, min(charge.confidence for charge in doc.charges))
        if not result.passed:
            doc.confidence = min(doc.confidence, settings.thresholds.low_confidence_below - 0.01)
    doc.validation = result
    return result


def review_reason_from_validation(validation: ValidationResult) -> str | None:
    for issue in validation.issues:
        if issue.action == "review" or issue.severity == "error":
            return RULE_TO_REASON.get(issue.rule, "OTHER")
    return None
