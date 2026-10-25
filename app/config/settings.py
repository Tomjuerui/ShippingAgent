"""配置装载：YAML + 环境变量覆盖（实现计划 §3.1 / WP-01）。

契约变更流程：修改 Settings 字段或 config.yaml 结构需登记版本号并通知编排者，
子智能体不得私改（WP-01）。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

try:  # .env 仅作开发便利，不存在时静默跳过
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def expand_env(value: Any) -> Any:
    """递归展开 ${VAR} / ${VAR:default}。未定义且无默认值时展开为空串。"""
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    return value


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


class LLMProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    base_url: str
    model: str
    api_key_env: str | None = None


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    profile: str = "local_proxy"
    profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.0
    timeout_seconds: int = 120
    max_retries: int = 2
    structured_output: str = "auto"
    cache_enabled: bool = True
    cache_dir: str = ".cache/llm"

    def active_profile(self) -> LLMProfile:
        name = os.environ.get("LLM_PROFILE") or self.profile
        if name not in self.profiles:
            raise KeyError(f"未知 LLM profile: {name}，可选 {sorted(self.profiles)}")
        profile = self.profiles[name].model_copy()
        if os.environ.get("LLM_BASE_URL"):
            profile.base_url = os.environ["LLM_BASE_URL"].rstrip("/")
        if os.environ.get("LLM_MODEL"):
            profile.model = os.environ["LLM_MODEL"]
        return profile

    def api_key(self) -> str | None:
        profile = self.active_profile()
        for env_name in (self.api_key_env, profile.api_key_env):
            if env_name and os.environ.get(env_name):
                return os.environ[env_name]
        return None


class ObjectStorageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: str = ""
    bucket: str = ""
    access_key_id_env: str = "OSS_AK"
    secret_key_env: str = "OSS_SK"
    region: str = ""
    ttl_minutes: int = 30

    def access_key(self) -> str | None:
        return os.environ.get(self.access_key_id_env) or None

    def secret_key(self) -> str | None:
        return os.environ.get(self.secret_key_env) or None

    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key() and self.secret_key())


class OCRConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = "http://36.103.199.11:5002/process"
    task: str = "orderChange"
    exposer: str = "auto"
    proxy_base_url: str = ""
    proxy_token_ttl_minutes: int = 30
    object_storage: ObjectStorageConfig = Field(default_factory=ObjectStorageConfig)
    temp_host_enabled: bool = False
    temp_host_url: str = "https://transfer.sh"
    retries: int = 3
    connect_timeout: int = 10
    read_timeout: int = 300
    min_text_chars: int = 40
    record_dir: str = "tests/fixtures/vcr"


class MatchingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    l4_enabled: bool = True
    l4_to_review: bool = True
    l4_allow_missing_container: bool = True
    amount_tolerance: float = 0.01
    date_window_days: int = 3
    derived_keys_enabled: bool = True
    fuzzy_ratio: int = 90


class ArapConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    policy: str = "reversed_posting"
    direction_ar_keywords: list[str] = Field(default_factory=list)
    direction_ap_keywords: list[str] = Field(default_factory=list)


class ThresholdsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_below: float = 0.70
    low_confidence_below: float = 0.85


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    output_dir: Path = Path("output")
    data_dir: Path = Path("data")
    cache_dir: Path = Path(".cache")


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8000
    upload_limit_mb: int = 20
    max_files_per_batch: int = 20
    concurrency: int = 3


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_eml_depth: int = 3
    max_text_chars_per_chunk: int = 12000
    enable_sqlite_checkpoint: bool = True


class AccountingEntityConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    aliases: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str = "1.0.0"
    prompt_version: str = "v1"


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    project_root: Path = Field(default_factory=default_project_root, exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    accounting_entity: AccountingEntityConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    arap: ArapConfig = Field(default_factory=ArapConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        self.paths.output_dir = _resolve_path(self.project_root, self.paths.output_dir)
        self.paths.data_dir = _resolve_path(self.project_root, self.paths.data_dir)
        self.paths.cache_dir = _resolve_path(self.project_root, self.paths.cache_dir)
        llm_cache = Path(self.llm.cache_dir)
        self.llm.cache_dir = str(_resolve_path(self.project_root, llm_cache))
        return self

    # ---- 常用目录 ----
    @property
    def output_dir(self) -> Path:
        return self.paths.output_dir

    @property
    def data_dir(self) -> Path:
        return self.paths.data_dir

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def bills_dir(self) -> Path:
        return self.data_dir / "bills"

    @property
    def dicts_dir(self) -> Path:
        return self.project_root / "app" / "governance" / "dicts"

    @property
    def static_dir(self) -> Path:
        return self.project_root / "app" / "web" / "static"

    @property
    def vcr_dir(self) -> Path:
        return _resolve_path(self.project_root, self.ocr.record_dir)

    def ensure_dirs(self) -> None:
        for path in (self.output_dir, self.uploads_dir, self.bills_dir, self.paths.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


def _env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """少量关键运行参数的快捷环境变量覆盖。"""
    raw.setdefault("ocr", {})
    raw.setdefault("llm", {})
    if os.environ.get("OCR_EXPOSER"):
        raw["ocr"]["exposer"] = os.environ["OCR_EXPOSER"]
    if os.environ.get("OCR_URL"):
        raw["ocr"]["url"] = os.environ["OCR_URL"]
    if os.environ.get("LLM_PROFILE"):
        raw["llm"]["profile"] = os.environ["LLM_PROFILE"]
    if os.environ.get("OCR_TEMP_HOST_ENABLED", "").lower() in {"1", "true", "yes"}:
        raw["ocr"]["temp_host_enabled"] = True
    return raw


def load(path: str | Path | None = None) -> Settings:
    """装载配置；默认读取项目根目录 config.yaml（可用 SBA_CONFIG 覆盖）。"""
    root = default_project_root()
    if load_dotenv is not None:
        load_dotenv(root / ".env", override=False)
    selected = path or os.environ.get("SBA_CONFIG") or None
    config_path = Path(selected) if selected else root / "config.yaml"
    config_path = config_path if config_path.is_absolute() else (root / config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = _env_overrides(expand_env(raw))
    settings = Settings(**raw, project_root=root, config_path=config_path.resolve())
    settings.ensure_dirs()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load()
