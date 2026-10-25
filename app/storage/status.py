"""批任务状态文件（Web 轮询 / 断点可观测）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.extraction.schemas import BatchStatus

_LOCK = threading.RLock()


class StatusStore:
    def __init__(self, output_dir: Path, batch_id: str):
        self.output_dir = output_dir
        self.batch_id = batch_id
        self.path = output_dir / batch_id / "status.json"

    def write(self, status: BatchStatus) -> None:
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self.path)

    def read(self) -> BatchStatus | None:
        if not self.path.exists():
            return None
        try:
            return BatchStatus.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def update(self, status: BatchStatus) -> None:
        self.write(status)
