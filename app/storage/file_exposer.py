"""多级文件外发策略链（D2 / WP-03）。

A 云对象存储预签名直链（首选）→ B 自托管文件代理 → C 输入 URL 直传 → D 公共图床（默认关）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from app.config.settings import Settings

# 1x1 PNG，仅用于策略连通性探测
_PROBE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ExposeError(RuntimeError):
    pass


@dataclass
class ProxyEntry:
    path: Path
    file_name: str
    expires_at: float


class ProxyRegistry:
    """B 级策略的 token → 本地文件映射，带 TTL。"""

    def __init__(self) -> None:
        self._entries: dict[str, ProxyEntry] = {}
        self._lock = threading.Lock()

    def register(self, path: Path, file_name: str, ttl_minutes: int) -> str:
        token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        with self._lock:
            self._entries[token] = ProxyEntry(
                path=path, file_name=file_name, expires_at=time.time() + ttl_minutes * 60
            )
        return token

    def resolve(self, token: str) -> ProxyEntry | None:
        with self._lock:
            entry = self._entries.get(token)
            if entry and entry.expires_at >= time.time():
                return entry
            if entry:
                self._entries.pop(token, None)
        return None

    def unregister(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def cleanup(self) -> int:
        now = time.time()
        with self._lock:
            expired = [token for token, entry in self._entries.items() if entry.expires_at < now]
            for token in expired:
                self._entries.pop(token, None)
        return len(expired)


PROXY_REGISTRY = ProxyRegistry()


@dataclass
class ExposeResult:
    url: str
    strategy: str
    cleanup: Callable[[], None] | None = None
    warnings: list[str] = field(default_factory=list)

    def close(self) -> None:
        if self.cleanup:
            try:
                self.cleanup()
            except Exception:
                pass
            self.cleanup = None


class FileExposer:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._selected: str | None = None
        self._selection_reason: str = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 策略可用性
    # ------------------------------------------------------------------ #
    def available_strategies(self) -> list[str]:
        available: list[str] = []
        if self.settings.ocr.object_storage.configured():
            available.append("s3")
        if self.settings.ocr.proxy_base_url:
            available.append("proxy")
        available.append("passthrough")
        if self.settings.ocr.temp_host_enabled:
            available.append("temp_host")
        return available

    def validate(self) -> tuple[str | None, str]:
        mode = (self.settings.ocr.exposer or "auto").lower()
        if mode == "auto":
            candidates = [s for s in self.available_strategies() if s != "passthrough"]
            if candidates:
                return candidates[0], f"auto 模式可用策略：{candidates}"
            return None, "auto 模式无可用策略（未配置云存储/自托管代理，公共图床默认关闭）"
        if mode in {"s3", "proxy", "temp_host", "passthrough"}:
            if mode in self.available_strategies():
                return mode, f"显式策略 {mode} 可用"
            return None, f"显式策略 {mode} 配置不完整或未启用"
        return None, f"未知 exposer 模式：{mode}"

    # ------------------------------------------------------------------ #
    # 选择（auto 模式可选真调 OCR 探测）
    # ------------------------------------------------------------------ #
    def select_strategy(self, probe: bool = False) -> str | None:
        with self._lock:
            if self._selected:
                return self._selected
            strategy, reason = self.validate()
            if strategy is None:
                self._selection_reason = reason
                return None
            if not probe or strategy == "passthrough":
                self._selected, self._selection_reason = strategy, reason
                return strategy
            for candidate in [s for s in self.available_strategies() if s in {"s3", "proxy", "temp_host"}]:
                ok, detail = self._probe(candidate)
                if ok:
                    self._selected = candidate
                    self._selection_reason = f"{candidate} 真调 OCR 探测通过：{detail}"
                    return candidate
            self._selection_reason = "auto 探测全部失败，OCR 文件外发不可用"
            return None

    def _probe(self, strategy: str) -> tuple[bool, str]:
        probe_path = self.settings.paths.cache_dir / "exposer_probe.png"
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_bytes(_PROBE_PNG)
        exposed = None
        try:
            exposed = self._expose_with(strategy, probe_path, "image/png")
            response = self.session.post(
                self.settings.ocr.url,
                json={
                    "file": exposed.url,
                    "task": self.settings.ocr.task,
                    "callBack": "",
                    "taskType": "",
                },
                # 探测覆盖"外发+OCR 全链路"，耗时可能远超普通请求，复用配置的超时
                timeout=(self.settings.ocr.connect_timeout, self.settings.ocr.read_timeout),
            )
            if response.status_code != 200:
                return False, f"OCR HTTP {response.status_code}"
            data = response.json()
            if isinstance(data, dict) and data.get("success") is False:
                return False, f"OCR 返回失败：{data.get('message') or data.get('error_code')}"
            # HTTP 200 + success 不代表链路可用：部分服务在无法抓取文件时
            # 也返回"成功 + 空内容"。必须确认探测文件确实被处理出内容。
            from app.agent.nodes.ocr_client import parse_ocr_payload  # 延迟导入避免环依赖

            result = parse_ocr_payload(data)
            if not (result.text.strip() or result.lines or result.tables):
                return False, "OCR 返回 200 但内容为空（服务可能无法抓取该 URL），探测不通过"
            return True, "HTTP 200 且内容非空"
        except Exception as exc:
            return False, str(exc)
        finally:
            if exposed:
                exposed.close()

    @property
    def selection_reason(self) -> str:
        return self._selection_reason

    # ------------------------------------------------------------------ #
    # 外发
    # ------------------------------------------------------------------ #
    def expose(self, source: str | Path, content_type: str | None = None) -> ExposeResult:
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            return ExposeResult(url=source, strategy="passthrough")
        path = Path(source)
        if not path.exists():
            raise ExposeError(f"待外发文件不存在：{path}")
        mode = (self.settings.ocr.exposer or "auto").lower()
        if mode == "auto":
            strategy = self.select_strategy(probe=True)
            if strategy in {"s3", "proxy", "temp_host"}:
                return self._expose_with(strategy, path, content_type)
            # 无可用外发策略时，若输入是 URL 会在上面提前返回
            raise ExposeError(self._selection_reason or "无可用文件外发策略")
        if mode == "passthrough":
            raise ExposeError("passthrough 策略要求输入本身是 URL")
        return self._expose_with(mode, path, content_type)

    def _expose_with(self, strategy: str, path: Path, content_type: str | None) -> ExposeResult:
        if strategy == "s3":
            return self._expose_s3(path, content_type)
        if strategy == "proxy":
            return self._expose_proxy(path)
        if strategy == "temp_host":
            return self._expose_temp_host(path)
        raise ExposeError(f"无法使用策略：{strategy}")

    def _expose_s3(self, path: Path, content_type: str | None) -> ExposeResult:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise ExposeError("缺少 boto3，无法使用 A 级云存储策略") from exc
        cfg = self.settings.ocr.object_storage
        client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key(),
            aws_secret_access_key=cfg.secret_key(),
            region_name=cfg.region or None,
            # 阿里云 OSS / 腾讯云 COS 新桶已禁用 path-style，必须 virtual-hosted 寻址；
            # botocore>=1.36 默认 STREAMING-UNSIGNED-PAYLOAD-TRAILER 上传，OSS 不支持，需改为按需校验
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        key = f"shipping-billing/{uuid.uuid4().hex}/{path.name}"
        warnings: list[str] = []
        try:
            client.put_object(
                Bucket=cfg.bucket,
                Key=key,
                Body=path.read_bytes(),
                ContentType=content_type or "application/octet-stream",
                ACL="private",
            )
        except Exception as exc:
            # 部分 S3 兼容实现（对象所有权/ACL 关闭）不接受 ACL 参数，依赖桶策略即可
            client.put_object(
                Bucket=cfg.bucket,
                Key=key,
                Body=path.read_bytes(),
                ContentType=content_type or "application/octet-stream",
            )
            warnings.append(f"对象存储不接受 ACL=private，已依赖私有桶策略：{exc}")
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.bucket, "Key": key},
            ExpiresIn=max(60, min(cfg.ttl_minutes, 30) * 60),
        )
        if cfg.ttl_minutes > 30:
            warnings.append("对象存储 TTL 超过 30 分钟，已按 30 分钟签发")

        def cleanup() -> None:
            try:
                client.delete_object(Bucket=cfg.bucket, Key=key)
            except Exception:
                pass

        return ExposeResult(url=url, strategy="s3", cleanup=cleanup, warnings=warnings)

    def _expose_proxy(self, path: Path) -> ExposeResult:
        base = self.settings.ocr.proxy_base_url.rstrip("/")
        token = PROXY_REGISTRY.register(
            path=path,
            file_name=path.name,
            ttl_minutes=min(self.settings.ocr.proxy_token_ttl_minutes, 30),
        )
        url = f"{base}/files/{token}/{quote(path.name)}"
        return ExposeResult(
            url=url,
            strategy="proxy",
            cleanup=lambda: PROXY_REGISTRY.unregister(token),
        )

    def _expose_temp_host(self, path: Path) -> ExposeResult:
        warnings = ["警告：正在使用公共临时图床（D 级），账单数据可能外泄且链接不稳定"]
        base = self.settings.ocr.temp_host_url.rstrip("/")
        response = self.session.put(
            f"{base}/{quote(path.name)}",
            data=path.read_bytes(),
            timeout=(10, 120),
        )
        response.raise_for_status()
        url = response.text.strip()
        if not url.startswith("http"):
            raise ExposeError(f"临时图床返回异常：{url[:200]}")
        return ExposeResult(url=url, strategy="temp_host", warnings=warnings)

    # ------------------------------------------------------------------ #
    # 健康检查（不真调 OCR）
    # ------------------------------------------------------------------ #
    def health(self) -> dict:
        strategy, reason = self.validate()
        return {
            "mode": self.settings.ocr.exposer,
            "available": self.available_strategies(),
            "selected": self._selected or strategy,
            "reason": self._selection_reason or reason,
            "temp_host_risk": self.settings.ocr.temp_host_enabled,
        }
