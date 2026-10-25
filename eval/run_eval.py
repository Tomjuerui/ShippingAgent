"""黄金集离线评测（PRD 第 9 节 / 专题 I / WP-10）。

fixtures 模式完全离线：用 golden/cases.json 中录制的 LLM/OCR 响应回放，
不调用任何外部服务。用法：
    python eval/run_eval.py --fixtures
    python eval/run_eval.py --fixtures --case pdf_update
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.graph import run_batch
from app.agent.nodes.ocr_client import OCRLine, OCRResult
from app.config.settings import load
from app.extraction.llm_client import LLMClient
from app.extraction.schemas import BatchOutput, BillRecord, DocumentExtraction

GOLDEN = Path(__file__).resolve().parent / "golden"
REPORTS = Path(__file__).resolve().parent / "reports"

GATES = {
    "field_f1": (">=", 0.95),
    "arap_accuracy": (">=", 0.98),
    "decision_accuracy": (">=", 0.95),
    "match_accuracy": (">=", 0.90),
    "record_count_accuracy": (">=", 0.95),
    "json_validity": ("==", 1.0),
    "hallucination_rate": ("<=", 0.0),
    "unexpected_review_rate": ("<=", 0.05),
}

_SCORE_FIELDS = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "original_charge_name",
    "charge_item",
    "amount",
    "currency",
    "payment_term",
    "posting_type",
    "operation",
    "match_level",
    "review_reason",
)


class RecordedBackend:
    def __init__(self, case: dict):
        self.case = case
        self.keywords = case.get("match_keywords") or []
        self.extraction = DocumentExtraction.model_validate(case["extraction"])

    def _pick(self, messages) -> DocumentExtraction:
        content = messages[-1]["content"] if messages else ""
        text = content.split("【文档文本】", 1)[-1]
        if any(keyword in text for keyword in self.keywords):
            return self.extraction.model_copy(deep=True)
        return DocumentExtraction()

    def structured(self, schema, messages, method):
        return self._pick(messages)

    def plain(self, messages):
        return json.dumps(self._pick(messages).model_dump(mode="json"), ensure_ascii=False)


class _DummyExposer:
    def health(self) -> dict:
        return {"selected": "fixture_ocr"}


class StubOCRClient:
    def __init__(self, config: dict):
        self.config = config
        self.exposer = _DummyExposer()

    def process(self, path, content_type=None) -> OCRResult:
        confidence = float(self.config.get("confidence", 0.95))
        lines = [
            OCRLine(text=text, page=1, confidence=confidence)
            for text in self.config.get("lines", [])
            if text
        ]
        return OCRResult(
            text="\n".join(line.text for line in lines),
            lines=lines,
            confidence=confidence,
            raw=self.config,
        )


def _flatten(record: BillRecord) -> dict:
    return {
        "document_type": record.document_info.document_type,
        "reference_no": record.document_info.reference_no,
        "document_date": record.document_info.document_date,
        "issuer": record.transaction_party.issuer,
        "recipient": record.transaction_party.recipient,
        "original_charge_name": record.charge_details.original_charge_name,
        "charge_item": record.charge_details.charge_item,
        "amount": record.charge_details.amount,
        "currency": record.charge_details.currency,
        "payment_term": record.charge_details.payment_term,
        "posting_type": record.financial_attributes.posting_type,
        "operation": record.operation.value,
        "match_level": record.match_info.match_level,
        "review_reason": record.review_reason,
    }


def _value_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= 0.01
    if expected is None or actual is None:
        return expected is actual or expected == actual
    return str(expected) == str(actual)


def _run_case(case: dict, settings) -> dict:
    client = LLMClient(settings, backend=RecordedBackend(case), use_cache=False)
    ocr = StubOCRClient(case["ocr"]) if case.get("ocr") else None
    file_path = GOLDEN / case["file"]
    bill_paths = [GOLDEN / item for item in case.get("bills", [])]
    error = None
    batch = None
    try:
        batch = run_batch(
            [file_path],
            bill_paths=bill_paths,
            batch_id=case["id"],
            settings=settings,
            llm_client=client,
            ocr_client=ocr,
            ignore_ledger=True,
            deterministic=True,
        )
    except Exception as exc:  # 评测不中断，记录失败
        error = f"{type(exc).__name__}: {exc}"
    return {"case": case, "batch": batch, "error": error}


def _compare(case: dict, batch: BatchOutput | None, error: str | None, settings) -> dict:
    expected_records = case["expected_records"]
    actual_records = list(batch.records) if batch else []
    mismatches: list[str] = []
    tp = fp = fn = 0
    decision_hits = arap_hits = match_hits = 0
    changed_hits = 0
    changed_total = 0
    json_failures = 0

    for index, expected in enumerate(expected_records):
        if index >= len(actual_records):
            for field in _SCORE_FIELDS:
                fn += 1
            mismatches.append(f"缺少记录 #{index + 1}")
            continue
        actual = _flatten(actual_records[index])
        for field in _SCORE_FIELDS:
            if field not in expected:
                continue
            if _value_equal(expected[field], actual.get(field)):
                tp += 1
            else:
                fp += 1
                fn += 1
                mismatches.append(
                    f"#{index + 1} {field}: 期望 {expected[field]!r} 实际 {actual.get(field)!r}"
                )
        decision_hits += int(expected.get("operation") == actual.get("operation"))
        arap_hits += int(_value_equal(expected.get("posting_type"), actual.get("posting_type")))
        match_hits += int(expected.get("match_level") == actual.get("match_level"))
        changed_required = expected.get("changed_fields_contains") or []
        if changed_required:
            changed_total += 1
            actual_fields = {
                item.field
                for item in (actual_records[index].change_info.changed_fields if actual_records[index].change_info else [])
            }
            if all(item in actual_fields for item in changed_required):
                changed_hits += 1
            else:
                mismatches.append(
                    f"#{index + 1} changed_fields 期望包含 {changed_required}，实际 {sorted(actual_fields)}"
                )

    for extra in actual_records[len(expected_records):]:
        for field in _SCORE_FIELDS:
            fp += 1
        mismatches.append(f"多出记录：{extra.record_id}")

    for record in actual_records:
        try:
            BillRecord.model_validate(record.model_dump(mode="json"))
        except Exception:
            json_failures += 1
    if batch is not None:
        try:
            path = settings.output_dir / batch.batch_id / "batch.json"
            BatchOutput.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            json_failures += 1

    hallucinations = sum(
        1
        for record in actual_records
        if any(
            "无法在原文中逐字溯源" in issue.message for issue in record.validation.issues
        )
    )
    review_count = sum(1 for record in actual_records if record.operation.value == "PENDING_REVIEW")
    unexpected_review = sum(
        1
        for index, record in enumerate(actual_records)
        if record.operation.value == "PENDING_REVIEW"
        and index < len(expected_records)
        and expected_records[index].get("operation") != "PENDING_REVIEW"
    )

    return {
        "case_id": case["id"],
        "error": error,
        "expected_records": len(expected_records),
        "actual_records": len(actual_records),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "decision_hits": decision_hits,
        "arap_hits": arap_hits,
        "match_hits": match_hits,
        "changed_hits": changed_hits,
        "changed_total": changed_total,
        "json_failures": json_failures,
        "hallucinations": hallucinations,
        "review_count": review_count,
        "unexpected_review": unexpected_review,
        "mismatches": mismatches,
        "operation_counts": (
            batch.summary.operation_counts.model_dump() if batch is not None else {}
        ),
        "expected_operations": [item.get("operation") for item in expected_records],
        "actual_operations": [record.operation.value for record in actual_records],
        "actual_postings": [
            record.financial_attributes.posting_type for record in actual_records
        ],
        "actual_amounts": [record.charge_details.amount for record in actual_records],
    }


def _metric(results: list[dict], key: str) -> float:
    return round(sum(result[key] for result in results), 6)


def aggregate(results: list[dict], total_records: int, total_expected: int) -> dict:
    tp = _metric(results, "tp")
    fp = _metric(results, "fp")
    fn = _metric(results, "fn")
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    reviewed = _metric(results, "review_count")
    unexpected = _metric(results, "unexpected_review")
    metrics = {
        "field_precision": round(precision, 4),
        "field_recall": round(recall, 4),
        "field_f1": round(f1, 4),
        "arap_accuracy": round(
            _metric(results, "arap_hits") / total_expected, 4
        ) if total_expected else 1.0,
        "decision_accuracy": round(
            _metric(results, "decision_hits") / total_expected, 4
        ) if total_expected else 1.0,
        "match_accuracy": round(
            _metric(results, "match_hits") / total_expected, 4
        ) if total_expected else 1.0,
        "record_count_accuracy": round(
            sum(1 for item in results if item["expected_records"] == item["actual_records"])
            / len(results),
            4,
        ) if results else 1.0,
        "json_validity": round(
            1.0
            - _metric(results, "json_failures")
            / (total_records + len(results))
            if total_records + len(results)
            else 1.0,
            4,
        ),
        "hallucination_rate": round(
            _metric(results, "hallucinations") / total_records, 4
        ) if total_records else 0.0,
        "review_rate": round(reviewed / total_records, 4) if total_records else 0.0,
        "unexpected_review_rate": round(
            unexpected / max(1, total_records - reviewed), 4
        ) if total_records else 0.0,
        "changed_field_accuracy": round(
            _metric(results, "changed_hits") / max(1, _metric(results, "changed_total")), 4
        ),
        "records": total_records,
        "expected_records": total_expected,
        "cases": len(results),
    }
    return metrics


def check_gates(metrics: dict) -> dict:
    outcomes = {}
    for key, (operator, threshold) in GATES.items():
        value = metrics[key]
        ok = value >= threshold if operator == ">=" else value == threshold if operator == "==" else value <= threshold
        outcomes[key] = {"value": value, "operator": operator, "threshold": threshold, "passed": ok}
    outcomes["changed_field_accuracy"] = {
        "value": metrics["changed_field_accuracy"],
        "operator": ">=",
        "threshold": 1.0,
        "passed": metrics["changed_field_accuracy"] >= 1.0,
    }
    return outcomes


def write_report(payload: dict, directory: Path | None = None) -> tuple[Path, Path]:
    target = Path(directory) if directory else REPORTS
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = target / f"eval-{stamp}.json"
    md_path = target / f"eval-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    metrics = payload["metrics"]
    lines = [
        "# Harness 评测报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 模式：{payload['mode']}",
        f"- 用例：{metrics['cases']}，记录：{metrics['records']}（期望 {metrics['expected_records']}）",
        f"- 总门禁：{'PASS' if payload['passed'] else 'FAIL'}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 门限 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for key, gate in payload["gates"].items():
        lines.append(
            f"| {key} | {gate['value']} | {gate['operator']} {gate['threshold']} | "
            f"{'PASS' if gate['passed'] else 'FAIL'} |"
        )
    lines.extend(["", "## 用例", "", "| 用例 | 记录 | 操作 | 期望操作 | 字段不匹配 |", "| --- | --- | --- | --- | --- |"])
    for case in payload["cases"]:
        lines.append(
            f"| {case['case_id']} | {case['actual_records']}/{case['expected_records']} | "
            f"{', '.join(case['actual_operations']) or '—'} | "
            f"{', '.join(str(item) for item in case['expected_operations'])} | "
            f"{len(case['mismatches'])} |"
        )
    for case in payload["cases"]:
        if case["mismatches"] or case["error"]:
            lines.extend(["", f"### {case['case_id']}", ""])
            if case["error"]:
                lines.append(f"- 运行错误：`{case['error']}`")
            for mismatch in case["mismatches"][:20]:
                lines.append(f"- {mismatch}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="黄金集评测（fixtures 模式离线）")
    parser.add_argument("--fixtures", action="store_true", help="使用录制响应回放（默认即离线）")
    parser.add_argument("--case", action="append", default=[], help="只跑指定用例，可多次")
    parser.add_argument("--report-dir", default=None, help="报告目录（默认 eval/reports）")
    args = parser.parse_args(argv)

    manifest = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            print(f"未找到用例：{sorted(wanted)}", file=sys.stderr)
            return 2

    # 交给 settings.load 解析 SBA_CONFIG，硬编码 config.yaml 会让 CI 找不到入库配置
    settings = load()
    results = []
    with tempfile.TemporaryDirectory(prefix="sba-eval-") as temp:
        temp_path = Path(temp)
        settings.paths.output_dir = temp_path / "output"
        settings.paths.data_dir = temp_path / "data"
        settings.llm.cache_dir = str(temp_path / "cache")
        settings.llm.cache_enabled = False
        settings.ensure_dirs()
        for case in cases:
            run = _run_case(case, settings)
            result = _compare(run["case"], run["batch"], run["error"], settings)
            results.append(result)
            status = "PASS" if not result["mismatches"] and not result["error"] else "FAIL"
            print(
                f"[{status}] {result['case_id']:<20} records={result['actual_records']}/"
                f"{result['expected_records']} ops={result['actual_operations']}"
            )
            for mismatch in result["mismatches"][:6]:
                print(f"    - {mismatch}")
            if result["error"]:
                print(f"    - ERROR {result['error']}")

    total_records = sum(result["actual_records"] for result in results)
    total_expected = sum(result["expected_records"] for result in results)
    metrics = aggregate(results, total_records, total_expected)
    gates = check_gates(metrics)
    passed = all(gate["passed"] for gate in gates.values()) and all(
        not result["error"] for result in results
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "fixtures",
        "metrics": metrics,
        "gates": gates,
        "passed": passed,
        "cases": results,
    }
    json_path, md_path = write_report(
        payload, Path(args.report_dir) if args.report_dir else None
    )

    print("\n指标：")
    for key, gate in gates.items():
        flag = "PASS" if gate["passed"] else "FAIL"
        print(f"  {key:<24} {gate['value']:<8} {gate['operator']} {gate['threshold']}  [{flag}]")
    print(f"\n报告：{json_path}")
    print(f"      {md_path}")
    print(f"总门禁：{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
