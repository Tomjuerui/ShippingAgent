"""FastAPI：REST API + 静态前端 + B 级自托管文件代理（D9 / FR-19~21 / WP-09）。"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.graph import run_batch
from app.config.settings import Settings, get_settings
from app.extraction.llm_client import LLMClient
from app.extraction.schemas import BatchStatus, BillRecord, Operation
from app.storage.file_exposer import PROXY_REGISTRY, FileExposer
from app.storage.outputs import (
    batch_dir,
    list_batch_outputs,
    load_batch_output,
    make_batch_id,
)
from app.storage.status import StatusStore

logger = logging.getLogger("shipping_billing")

ALLOWED_INPUT_EXT = {
    ".eml", ".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".xls", ".xlsx", ".doc", ".docx",
}
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_batch_id(batch_id: str) -> str:
    if not _BATCH_ID_RE.match(batch_id) or ".." in batch_id:
        raise HTTPException(status_code=400, detail="非法 batch_id")
    return batch_id


def _sanitize_name(name: str) -> str:
    base = Path(name or "upload").name
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._\- ]+", "_", base).strip(" ._")
    return base[:150] or "upload"


def _unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


async def _save_upload(upload: UploadFile, directory: Path, limit_mb: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = _unique_path(directory, _sanitize_name(upload.filename or "upload"))
    limit = limit_mb * 1024 * 1024
    size = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件 {upload.filename} 超过 {limit_mb} MB 限制",
                    )
                fh.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"空文件：{upload.filename}")
    return target


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        llm_factory: Callable[[], LLMClient] | None = None,
        inline: bool = False,
    ):
        self.settings = settings
        self.llm_factory = llm_factory
        self.inline = inline
        self.executor = (
            None if inline else ThreadPoolExecutor(max_workers=settings.web.concurrency)
        )
        self._lock = threading.Lock()

    def submit(self, batch_id: str, input_paths: list[Path], bill_paths: list[Path]) -> None:
        if self.inline:
            self._run(batch_id, input_paths, bill_paths)
        else:
            self.executor.submit(self._run, batch_id, input_paths, bill_paths)  # type: ignore[union-attr]

    def _run(self, batch_id: str, input_paths: list[Path], bill_paths: list[Path]) -> None:
        try:
            client = self.llm_factory() if self.llm_factory else None
            run_batch(
                [str(path) for path in input_paths],
                bill_paths=[str(path) for path in bill_paths],
                batch_id=batch_id,
                settings=self.settings,
                llm_client=client,
            )
        except Exception as exc:  # run_batch 已尽力写失败状态，此处兜底
            logger.exception("批次 %s 处理失败", batch_id)
            store = StatusStore(self.settings.output_dir, batch_id)
            status = store.read() or BatchStatus(
                batch_id=batch_id,
                status="failed",
                created_at="",
                progress=0,
            )
            status.status = "failed"
            status.error = f"{type(exc).__name__}: {exc}"
            status.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            store.write(status)


def _merge_status(output_dir: Path, batch_id: str) -> dict:
    store = StatusStore(output_dir, batch_id)
    status = store.read()
    output = load_batch_output(output_dir, batch_id)
    if status is None and output is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if status is None and output is not None:
        status = BatchStatus(
            batch_id=batch_id,
            status="completed",
            progress=100,
            total_files=len(output.files),
            processed_files=len(output.files),
            created_at=output.processed_at,
            finished_at=output.processed_at,
        )
    data = status.model_dump(mode="json") if status else {"batch_id": batch_id}
    if output is not None:
        data["summary"] = output.summary.model_dump(mode="json")
        data["meta"] = output.meta.model_dump(mode="json")
        data["record_count"] = len(output.records)
        data["file_results"] = [item.model_dump(mode="json") for item in output.files]
        if not data.get("files"):
            data["files"] = [
                {
                    "file_name": item.file_name,
                    "file_sha256": item.file_sha256,
                    "status": item.status,
                    "stage": "completed" if item.status == "completed" else item.status,
                    "record_count": item.record_count,
                    "review_reason": item.review_reason,
                    "message": item.message,
                }
                for item in output.files
            ]
    return data


def _health_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def create_app(
    settings: Settings | None = None,
    llm_factory: Callable[[], LLMClient] | None = None,
    inline: bool = False,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    runner = JobRunner(settings, llm_factory=llm_factory, inline=inline)
    stop_event = threading.Event()

    def _cleanup_proxy_tokens() -> None:
        while not stop_event.wait(60):
            PROXY_REGISTRY.cleanup()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = threading.Thread(target=_cleanup_proxy_tokens, daemon=True)
        thread.start()
        # 进程重启后把未完成批次标记为失败（文件哈希账本支持重跑）
        for child in settings.output_dir.iterdir() if settings.output_dir.exists() else []:
            if not child.is_dir():
                continue
            store = StatusStore(settings.output_dir, child.name)
            status = store.read()
            if status and status.status in {"queued", "running"}:
                status.status = "failed"
                status.error = "服务重启，任务中断；可重新上传（同文件哈希会被识别）"
                store.write(status)
        yield
        stop_event.set()
        if runner.executor is not None:
            runner.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="航运智能账单助手", version=settings.project.version, lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.get("/api/health")
    def health(probe: bool = Query(False, description="是否真调 OCR/LLM 检查连通性")):
        exposer = FileExposer(settings)
        ocr = exposer.health()
        try:
            profile = settings.llm.active_profile()
            profile_name = os.environ.get("LLM_PROFILE") or settings.llm.profile
            llm = {
                "profile": profile_name,
                "base_url": profile.base_url,
                "model": profile.model,
                "key_present": bool(settings.llm.api_key()),
                "reachable": None,
            }
        except Exception as exc:
            llm = {"reachable": False, "error": str(exc)}
        ocr["reachable"] = None
        if probe:
            try:
                response = requests.get(_health_url(settings.ocr.url), timeout=(3, 5))
                ocr["reachable"] = response.status_code == 200
            except Exception:
                ocr["reachable"] = False
            if llm.get("key_present"):
                try:
                    response = requests.get(
                        f"{llm['base_url']}/models",
                        headers={"Authorization": f"Bearer {settings.llm.api_key()}"},
                        timeout=(3, 6),
                    )
                    llm["reachable"] = response.status_code == 200
                except Exception:
                    llm["reachable"] = False
        return {
            "status": "ok",
            "version": settings.project.version,
            "accounting_entity": settings.accounting_entity.name,
            "ocr": ocr,
            "llm": llm,
            "temp_host_risk": settings.ocr.temp_host_enabled,
        }

    @app.post("/api/batches", status_code=202)
    async def create_batch(
        files: list[UploadFile] = File(default=[]),
        bills: list[UploadFile] = File(default=[]),
        files_bracket: list[UploadFile] = File(default=[], alias="files[]"),
        bills_bracket: list[UploadFile] = File(default=[], alias="bills[]"),
    ):
        documents = list(files) + list(files_bracket)
        bill_files = list(bills) + list(bills_bracket)
        if not documents:
            raise HTTPException(status_code=400, detail="请至少上传一个账单文件")
        if len(documents) > settings.web.max_files_per_batch:
            raise HTTPException(
                status_code=400,
                detail=f"单批最多 {settings.web.max_files_per_batch} 个文件",
            )
        batch_id = make_batch_id()
        upload_dir = settings.uploads_dir / batch_id
        bill_dir = settings.bills_dir / batch_id
        input_paths: list[Path] = []
        bill_paths: list[Path] = []
        for upload in documents:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in ALLOWED_INPUT_EXT:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的格式：{upload.filename}（允许 {sorted(ALLOWED_INPUT_EXT)}）",
                )
        for upload in bill_files:
            if Path(upload.filename or "").suffix.lower() != ".json":
                raise HTTPException(status_code=400, detail="已有账单必须是 JSON 文件")
        try:
            for upload in documents:
                input_paths.append(
                    await _save_upload(upload, upload_dir, settings.web.upload_limit_mb)
                )
            for upload in bill_files:
                bill_paths.append(
                    await _save_upload(upload, bill_dir, settings.web.upload_limit_mb)
                )
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            shutil.rmtree(bill_dir, ignore_errors=True)
            raise
        if not input_paths:
            shutil.rmtree(upload_dir, ignore_errors=True)
            shutil.rmtree(bill_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="没有有效上传文件")
        StatusStore(settings.output_dir, batch_id).write(
            BatchStatus(
                batch_id=batch_id,
                status="queued",
                progress=0,
                total_files=len(input_paths),
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                files=[],
            )
        )
        runner.submit(batch_id, input_paths, bill_paths)
        return {"batch_id": batch_id, "status": "queued", "files": len(input_paths)}

    @app.get("/api/batches")
    def list_batches():
        items = {item["batch_id"]: item for item in list_batch_outputs(settings.output_dir)}
        if settings.output_dir.exists():
            for child in settings.output_dir.iterdir():
                if child.is_dir() and (child / "status.json").exists():
                    items.setdefault(child.name, {"batch_id": child.name})
        result = []
        for batch_id, item in items.items():
            try:
                data = _merge_status(settings.output_dir, batch_id)
            except HTTPException:
                continue
            result.append(
                {
                    "batch_id": batch_id,
                    "status": data.get("status"),
                    "progress": data.get("progress", 0),
                    "processed_at": data.get("finished_at") or data.get("created_at"),
                    "summary": data.get("summary", {}),
                    "total_files": data.get("total_files", 0),
                }
            )
        result.sort(key=lambda item: item.get("processed_at") or "", reverse=True)
        return {"batches": result}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str):
        return _merge_status(settings.output_dir, _safe_batch_id(batch_id))

    def _records(batch_id: str) -> tuple[list[BillRecord], object]:
        output = load_batch_output(settings.output_dir, batch_id)
        if output is None:
            _merge_status(settings.output_dir, batch_id)
            raise HTTPException(status_code=404, detail="批次尚未产出记录")
        return output.records, output

    @app.get("/api/batches/{batch_id}/records")
    def get_records(
        batch_id: str,
        operation: str | None = Query(None, description="CREATE/UPDATE/NOCHANGE/PENDING_REVIEW，可逗号分隔"),
        low_confidence: bool | None = Query(None),
    ):
        batch_id = _safe_batch_id(batch_id)
        records, output = _records(batch_id)
        operations = None
        if operation:
            operations = {item.strip().upper() for item in operation.split(",") if item.strip()}
            invalid = operations - {item.value for item in Operation}
            if invalid:
                raise HTTPException(status_code=400, detail=f"未知 operation：{sorted(invalid)}")
        filtered = [
            record
            for record in records
            if (operations is None or record.operation.value in operations)
            and (low_confidence is None or record.low_confidence == low_confidence)
        ]
        return {
            "batch_id": batch_id,
            "summary": output.summary.model_dump(mode="json"),
            "meta": output.meta.model_dump(mode="json"),
            "files": [item.model_dump(mode="json") for item in output.files],
            "records": [record.model_dump(mode="json") for record in filtered],
            "filtered": len(filtered),
            "total": len(records),
        }

    @app.get("/api/batches/{batch_id}/export")
    def export_batch(batch_id: str):
        batch_id = _safe_batch_id(batch_id)
        path = batch_dir(settings.output_dir, batch_id) / "batch.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="批次产物不存在")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"{batch_id}.json",
        )

    @app.get("/files/{token}/{filename}")
    def serve_proxy_file(token: str, filename: str):
        entry = PROXY_REGISTRY.resolve(token)
        if entry is None:
            raise HTTPException(status_code=404, detail="链接不存在或已过期")
        if not entry.path.exists():
            raise HTTPException(status_code=404, detail="文件已清理")
        media_type = mimetypes.guess_type(entry.file_name)[0] or "application/octet-stream"
        return FileResponse(entry.path, media_type=media_type, filename=entry.file_name)

    @app.get("/")
    def index():
        return FileResponse(settings.static_dir / "index.html")

    if settings.static_dir.exists():
        app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run(app, host=_settings.web.host, port=_settings.web.port)
