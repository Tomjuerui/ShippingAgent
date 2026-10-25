"""LLM 端点自检（Phase 0 / D8）。

用法：
    python -m app.tools.llm_selfcheck --dry-run
    python -m app.tools.llm_selfcheck
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

from app.config.settings import load


def build_report(dry_run: bool) -> dict:
    settings = load()
    profile_name = os.environ.get("LLM_PROFILE") or settings.llm.profile
    profile = settings.llm.active_profile()
    report = {
        "dry_run": dry_run,
        "profile": profile_name,
        "base_url": profile.base_url,
        "model": profile.model,
        "api_key_present": bool(settings.llm.api_key()),
        "structured_output": settings.llm.structured_output,
    }
    if dry_run:
        return report
    key = settings.llm.api_key()
    if not key:
        report["error"] = "缺少 API key（LLM_API_KEY 或 profile 对应环境变量）"
        return report
    headers = {"Authorization": f"Bearer {key}"}
    started = time.perf_counter()
    try:
        models = requests.get(f"{profile.base_url}/models", headers=headers, timeout=(10, 30))
        report["models_http"] = models.status_code
        if models.status_code == 200:
            report["models"] = [item.get("id") for item in models.json().get("data", [])][:20]
        response = requests.post(
            f"{profile.base_url}/chat/completions",
            headers=headers,
            json={
                "model": profile.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "只输出 JSON：{\"ok\": true}"},
                    {"role": "user", "content": "ping"},
                ],
            },
            timeout=(10, 60),
        )
        report["chat_http"] = response.status_code
        if response.status_code == 200:
            content = response.json()["choices"][0]["message"]["content"]
            report["chat_content"] = content[:500]
        else:
            report["chat_error"] = response.text[:500]
    except Exception as exc:
        report["error"] = str(exc)
    report["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 端点自检")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if args.dry_run or report.get("chat_http") == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
