"""CLI 批处理入口：python -m app.cli <input...> [--bills DIR]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.agent.graph import run_batch
from app.config.settings import load
from app.extraction.llm_client import LLMClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="航运智能账单助手 CLI")
    parser.add_argument("inputs", nargs="+", help="输入文件或目录（EML/PDF/图片/Excel/Word）")
    parser.add_argument("--bills", nargs="*", default=[], help="已有账单 JSON 文件或目录")
    parser.add_argument("--batch-id", default=None, help="指定批次 ID（默认自动生成）")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 config.yaml 的 output）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--llm-profile", default=None, help="local_proxy | deepseek_official | openai")
    parser.add_argument("--no-cache", action="store_true", help="禁用 LLM 响应缓存")
    parser.add_argument("--deterministic", action="store_true", help="确定性时间戳（幂等测试/评测）")
    parser.add_argument("--ignore-ledger", action="store_true", help="忽略历史哈希账本，强制重跑")
    parser.add_argument("--json", action="store_true", help="向 stdout 输出完整批 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.llm_profile:
        os.environ["LLM_PROFILE"] = args.llm_profile
    settings = load(args.config)
    client = None
    if args.no_cache:
        client = LLMClient(settings, use_cache=False)
    try:
        batch = run_batch(
            input_paths=args.inputs,
            bill_paths=args.bills,
            batch_id=args.batch_id,
            settings=settings,
            output_dir=args.output_dir,
            llm_client=client,
            deterministic=args.deterministic,
            ignore_ledger=args.ignore_ledger,
        )
    except Exception as exc:
        print(f"处理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        batch_id = args.batch_id or "(自动)"
        print(f"批状态见 output/<batch_id>/status.json（batch_id={batch_id}）", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        output_dir = Path(args.output_dir) if args.output_dir else settings.output_dir
        print(f"批次：{batch.batch_id}")
        print(f"输入文件：{batch.summary.input_files}，失败：{batch.summary.failed_files}，跳过：{batch.summary.skipped_files}")
        print(f"记录：{batch.summary.records}  {batch.summary.operation_counts.model_dump()}")
        if batch.summary.review_reasons:
            print(f"复核原因：{batch.summary.review_reasons}")
        print(f"产物：{output_dir / batch.batch_id / 'batch.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
