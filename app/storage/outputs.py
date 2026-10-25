"""输出落盘与批包装（FR-18 / PRD 6.5 / WP-08）。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.extraction.schemas import BatchOutput, BillRecord, Operation


def make_batch_id(prefix: str = "batch") -> str:
    now = datetime.now()
    return f"{prefix}-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def now_iso(deterministic: bool = False) -> str:
    if deterministic or os.environ.get("SBA_DETERMINISTIC", "").lower() in {"1", "true", "yes"}:
        epoch = os.environ.get("SOURCE_DATE_EPOCH", "0")
        try:
            stamp = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (TypeError, ValueError):
            stamp = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return stamp.isoformat()
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def batch_dir(output_dir: Path, batch_id: str) -> Path:
    return output_dir / batch_id


def write_outputs(batch: BatchOutput, output_dir: Path) -> dict[str, Path]:
    directory = batch_dir(output_dir, batch.batch_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = batch.model_dump(mode="json")
    _atomic_write(
        directory / "batch.json", json.dumps(payload, ensure_ascii=False, indent=2)
    )
    _atomic_write(
        directory / "records.json",
        json.dumps(payload["records"], ensure_ascii=False, indent=2),
    )
    _atomic_write(
        directory / "summary.json",
        json.dumps(
            {
                "batch_id": payload["batch_id"],
                "processed_at": payload["processed_at"],
                "summary": payload["summary"],
                "files": payload["files"],
                "meta": payload["meta"],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return {
        "batch": directory / "batch.json",
        "records": directory / "records.json",
        "summary": directory / "summary.json",
    }


def load_batch_output(output_dir: Path, batch_id: str) -> BatchOutput | None:
    path = batch_dir(output_dir, batch_id) / "batch.json"
    if not path.exists():
        return None
    try:
        return BatchOutput.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def list_batch_outputs(output_dir: Path) -> list[dict]:
    if not output_dir.exists():
        return []
    items: list[dict] = []
    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        path = child / "batch.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append(
                {
                    "batch_id": data.get("batch_id", child.name),
                    "processed_at": data.get("processed_at"),
                    "summary": data.get("summary", {}),
                    "status": data.get("status", "completed"),
                }
            )
        except Exception:
            continue
    items.sort(key=lambda item: item.get("processed_at") or "", reverse=True)
    return items


# --------------------------------------------------------------------------- #
# 幂等账本（NFR-02 断点续跑 / FR-15 重复提交）
# --------------------------------------------------------------------------- #
def ledger_path(output_dir: Path) -> Path:
    return output_dir / "ledger.json"


def load_ledger(output_dir: Path) -> dict[str, str]:
    path = ledger_path(output_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def save_ledger(output_dir: Path, ledger: dict[str, str]) -> None:
    _atomic_write(
        ledger_path(output_dir), json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True)
    )


def clone_duplicate_records(
    previous: BatchOutput, file_sha256: str, previous_batch_id: str
) -> list[BillRecord]:
    clones: list[BillRecord] = []
    for index, record in enumerate(
        [r for r in previous.records if (r.source.file_sha256 or "") == file_sha256], start=1
    ):
        clone = record.model_copy(deep=True)
        clone.record_id = f"{file_sha256[:8]}-{index:03d}"
        if record.operation == Operation.PENDING_REVIEW:
            clone.operation = Operation.PENDING_REVIEW
            clone.review_reason = record.review_reason
            clone.low_confidence = record.low_confidence
        else:
            clone.operation = Operation.NOCHANGE
            clone.low_confidence = False
            clone.review_reason = None
        clone.change_info = None
        clone.audit_trail.ai_reasoning = list(clone.audit_trail.ai_reasoning) + [
            f"重复提交（DUPLICATE_SUBMISSION）：与批次 {previous_batch_id} 的文件哈希相同，未重复入账"
        ]
        clones.append(clone)
    return clones
