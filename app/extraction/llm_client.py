"""LLM 多端点接入 + 结构化输出降级链 + 响应缓存（专题 C / D8 / WP-04）。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.config.settings import Settings
from app.extraction.schemas import DocumentExtraction
from app.governance.dictionaries import load_dicts


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


class SchemaRetryExceeded(LLMError):
    blacklist_error: str = ""

    def __init__(self, message: str, last_error: Exception | str | None = None):
        super().__init__(message)
        self.last_error = last_error


@dataclass
class ExtractionCallResult:
    extraction: DocumentExtraction
    mode: str
    cached: bool
    model: str
    prompt_version: str


class LLMBackend(Protocol):
    def structured(self, schema: type, messages: list[dict], method: str) -> Any: ...

    def plain(self, messages: list[dict]) -> str: ...


class LangChainBackend:
    """默认 LangChain 后端；测试可注入 FakeBackend 完全离线。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None

    def _get_model(self):
        if self._model is not None:
            return self._model
        key = self.settings.llm.api_key()
        if not key:
            raise LLMUnavailable(
                "未设置 LLM_API_KEY（或当前 profile 对应的密钥环境变量），且缓存未命中"
            )
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("未安装 langchain-openai") from exc
        profile = self.settings.llm.active_profile()
        self._model = ChatOpenAI(
            model=profile.model,
            api_key=key,
            base_url=profile.base_url,
            temperature=self.settings.llm.temperature,
            timeout=self.settings.llm.timeout_seconds,
            max_retries=0,
        )
        return self._model

    @staticmethod
    def _to_lc(messages: list[dict]):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        mapping = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
        return [mapping.get(m["role"], HumanMessage)(content=m["content"]) for m in messages]

    def structured(self, schema: type, messages: list[dict], method: str) -> Any:
        model = self._get_model()
        runnable = model.with_structured_output(schema, method=method)
        return runnable.invoke(self._to_lc(messages))

    def plain(self, messages: list[dict]) -> str:
        model = self._get_model()
        response = model.invoke(self._to_lc(messages))
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return str(content)


_UNAVAILABLE_HINTS = (
    "connection",
    "connect",
    "timed out",
    "timeout",
    "ssl",
    "proxy",
    "401",
    "403",
    "unauthorized",
    "authentication",
    "api key",
    "api_key",
    "invalid_api_key",
    "not found",
    "name or service",
    "未设置",
    "llm 不可用",
)


def _looks_unavailable(exc: Exception) -> bool:
    if isinstance(exc, LLMUnavailable):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in _UNAVAILABLE_HINTS)


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text[start:])
            return value
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValueError("模型输出中未找到合法 JSON 对象")


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        backend: LLMBackend | None = None,
        cache_dir: str | Path | None = None,
        use_cache: bool | None = None,
    ):
        self.settings = settings
        self.backend = backend or LangChainBackend(settings)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(settings.llm.cache_dir)
        self.use_cache = settings.llm.cache_enabled if use_cache is None else use_cache
        self.prompt_version = settings.project.prompt_version
        self.prompt = self._load_prompt()

    # ------------------------------------------------------------------ #
    # Prompt / 缓存
    # ------------------------------------------------------------------ #
    def _load_prompt(self) -> str:
        path = (
            self.settings.project_root
            / "app"
            / "extraction"
            / "prompts"
            / f"{self.prompt_version}_extract.txt"
        )
        if path.exists():
            return path.read_text(encoding="utf-8")
        return (
            "你是航运账单结构化抽取器。只输出 JSON，不得编造；"
            "字段不存在的填 null。输出必须符合给定 schema。"
        )

    def cache_key(self, text: str, model: str | None = None) -> str:
        profile = self.settings.llm.active_profile()
        material = "|".join(
            [
                self.prompt_version,
                model or profile.model,
                profile.base_url,
                load_dicts().version,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> ExtractionCallResult | None:
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            extraction = DocumentExtraction.model_validate(payload["extraction"])
        except Exception:
            return None
        profile = self.settings.llm.active_profile()
        return ExtractionCallResult(
            extraction=extraction,
            mode="cache",
            cached=True,
            model=profile.model,
            prompt_version=self.prompt_version,
        )

    def _write_cache(self, key: str, extraction: DocumentExtraction, mode: str) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "extraction": extraction.model_dump(mode="json"),
            "mode": mode,
            "model": self.settings.llm.active_profile().model,
            "prompt_version": self.prompt_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        target = self._cache_path(key)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)

    # ------------------------------------------------------------------ #
    # 消息
    # ------------------------------------------------------------------ #
    def _messages(self, text: str, source_name: str, extra_instruction: str = "") -> list[dict]:
        user = f"【来源文件】{source_name or 'unknown'}\n【文档文本】\n{text}"
        if extra_instruction:
            user = f"{user}\n\n{extra_instruction}"
        return [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": user},
        ]

    def _validate(self, value: Any, chunk_id: str | None) -> DocumentExtraction:
        if isinstance(value, DocumentExtraction):
            extraction = value
        elif isinstance(value, dict):
            extraction = DocumentExtraction.model_validate(value)
        elif hasattr(value, "model_dump"):
            extraction = DocumentExtraction.model_validate(value.model_dump())
        else:
            raise ValueError(f"结构化输出类型异常：{type(value)}")
        if not extraction.extraction_id:
            extraction.extraction_id = uuid.uuid4().hex[:12]
        extraction.source_chunk_id = chunk_id
        extraction.charges = [
            charge
            for charge in extraction.charges
            if charge.original_charge_name or charge.amount is not None
        ]
        return extraction

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def extract(
        self, text: str, source_name: str = "", chunk_id: str | None = None
    ) -> ExtractionCallResult:
        if not text.strip():
            extraction = DocumentExtraction(
                extraction_id=uuid.uuid4().hex[:12], source_chunk_id=chunk_id
            )
            return ExtractionCallResult(
                extraction,
                "empty",
                False,
                self.settings.llm.active_profile().model,
                self.prompt_version,
            )

        key = self.cache_key(text)
        cached = self._read_cache(key)
        if cached:
            cached.extraction.source_chunk_id = chunk_id
            return cached

        mode_setting = (self.settings.llm.structured_output or "auto").lower()
        schema = DocumentExtraction
        messages = self._messages(text, source_name)
        errors: list[str] = []

        if mode_setting in {"auto", "function_calling"}:
            try:
                value = self.backend.structured(schema, messages, "function_calling")
                extraction = self._validate(value, chunk_id)
                self._write_cache(key, extraction, "function_calling")
                return ExtractionCallResult(
                    extraction,
                    "function_calling",
                    False,
                    self.settings.llm.active_profile().model,
                    self.prompt_version,
                )
            except Exception as exc:
                if _looks_unavailable(exc):
                    raise LLMUnavailable(f"LLM 不可用：{exc}") from exc
                errors.append(f"function_calling: {exc}")

        if mode_setting in {"auto", "json_mode"}:
            try:
                value = self.backend.structured(schema, messages, "json_mode")
                extraction = self._validate(value, chunk_id)
                self._write_cache(key, extraction, "json_mode")
                return ExtractionCallResult(
                    extraction,
                    "json_mode",
                    False,
                    self.settings.llm.active_profile().model,
                    self.prompt_version,
                )
            except Exception as exc:
                if _looks_unavailable(exc):
                    raise LLMUnavailable(f"LLM 不可用：{exc}") from exc
                errors.append(f"json_mode: {exc}")

        # 兜底：提示词内嵌 schema + 本地校验 + 错误回喂重试
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        attempts = max(1, self.settings.llm.max_retries + 1)
        retry_messages = self._messages(
            text,
            source_name,
            extra_instruction=(
                "你必须只输出一个 JSON 对象，且严格符合以下 JSON Schema：\n" + schema_text
            ),
        )
        last_error: Exception | str | None = None
        last_content = ""
        for attempt in range(attempts):
            try:
                content = self.backend.plain(retry_messages)
                last_content = content
                value = _extract_json(content)
                extraction = self._validate(value, chunk_id)
                self._write_cache(key, extraction, "prompt_schema")
                return ExtractionCallResult(
                    extraction,
                    "prompt_schema",
                    False,
                    self.settings.llm.active_profile().model,
                    self.prompt_version,
                )
            except Exception as exc:
                if _looks_unavailable(exc):
                    raise LLMUnavailable(f"LLM 不可用：{exc}") from exc
                last_error = exc
                errors.append(f"prompt_schema#{attempt + 1}: {exc}")
                if attempt < attempts - 1:
                    retry_messages = retry_messages + [
                        {"role": "assistant", "content": last_content[:4000]},
                        {
                            "role": "user",
                            "content": (
                                "上一个输出无法通过 JSON Schema 校验："
                                f"{exc}。请只输出修正后的 JSON 对象，不要解释。"
                            ),
                        },
                    ]
        raise SchemaRetryExceeded(
            "结构化输出降级链全部失败：" + " | ".join(errors[-4:]), last_error=last_error
        )
