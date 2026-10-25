"""文件外发策略链连通性自检（WP-03 / 部署验收第一步）。

用法：
    python -m app.tools.ocr_selfcheck --dry-run   # 只评估配置，不发网络请求
    python -m app.tools.ocr_selfcheck             # 真调 OCR 健康检查 + 策略探测
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

import requests

from app.config.settings import load
from app.storage.file_exposer import FileExposer


def _ocr_health_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def build_report(dry_run: bool) -> dict:
    settings = load()
    exposer = FileExposer(settings)
    strategy, reason = exposer.validate()
    report = {
        "dry_run": dry_run,
        "mode": settings.ocr.exposer,
        "ocr_url": settings.ocr.url,
        "available": exposer.available_strategies(),
        "selected_without_probe": strategy,
        "reason": reason,
        "temp_host_risk": settings.ocr.temp_host_enabled,
        "details": {
            "s3": {
                "configured": settings.ocr.object_storage.configured(),
                "endpoint": settings.ocr.object_storage.endpoint or None,
                "bucket": settings.ocr.object_storage.bucket or None,
                "ttl_minutes": settings.ocr.object_storage.ttl_minutes,
            },
            "proxy": {
                "configured": bool(settings.ocr.proxy_base_url),
                "base_url": settings.ocr.proxy_base_url or None,
            },
            "passthrough": {"condition": "输入文件本身是 URL 时可用"},
            "temp_host": {
                "enabled": settings.ocr.temp_host_enabled,
                "url": settings.ocr.temp_host_url,
            },
        },
    }
    if dry_run:
        return report
    try:
        response = requests.get(_ocr_health_url(settings.ocr.url), timeout=(5, 10))
        report["ocr_health"] = {"status_code": response.status_code, "ok": response.status_code == 200}
    except Exception as exc:
        report["ocr_health"] = {"ok": False, "error": str(exc)}
    if report["ocr_health"].get("ok"):
        selected = exposer.select_strategy(probe=True)
        report["selected_after_probe"] = selected
        report["probe_reason"] = exposer.selection_reason
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR 文件外发策略链自检")
    parser.add_argument("--dry-run", action="store_true", help="只评估配置，不发网络请求")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    args = parser.parse_args(argv)
    report = build_report(args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0
    return 0 if report.get("selected_after_probe") else 1


if __name__ == "__main__":
    sys.exit(main())
