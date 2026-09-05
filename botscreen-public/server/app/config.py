"""Application configuration with validation and secret references.

Secrets are never stored here. In production, missing required secret
references cause startup to fail.
"""

from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

SUPPORTED_MODALITIES = ("text", "audio", "image", "video")


def _is_loopback_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower()
    except ValueError:
        return True
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


class CloudProviderConfig(BaseModel):
    provider: str = "dashscope_realtime"
    adapter: str = "cloud_realtime"
    model: str = "qwen3.5-omni-plus-realtime"
    api_base: str = "https://dashscope.aliyuncs.com/api/v1"
    api_key_env: str = "GCMW_CLOUD_API_KEY"
    transport: str = "websocket"
    modalities: tuple[str, ...] = ("text", "audio", "image", "video")
    timeout_ms: int = Field(15_000, gt=0)
    connect_timeout_ms: int = Field(5_000, gt=0)

    @field_validator("modalities")
    @classmethod
    def validate_modalities(cls, v):
        if not v or any(item not in SUPPORTED_MODALITIES for item in v):
            raise ValueError(
                f"modalities must be non-empty subset of {SUPPORTED_MODALITIES}"
            )
        return v


class LocalProviderConfig(BaseModel):
    provider: str = "vllm"
    adapter: str = "local_omni"
    model: str = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    api_base: str = "http://127.0.0.1:8001/v1"
    api_key_env: str | None = None
    modalities: tuple[str, ...] = ("text", "audio", "image", "video")
    timeout_ms: int = Field(30_000, gt=0)
    connect_timeout_ms: int = Field(5_000, gt=0)

    @field_validator("modalities")
    @classmethod
    def validate_modalities(cls, v):
        if not v or any(item not in SUPPORTED_MODALITIES for item in v):
            raise ValueError(
                f"modalities must be non-empty subset of {SUPPORTED_MODALITIES}"
            )
        return v


class Settings(BaseModel):
    app_name: str = "gcmw-agent"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    active_provider: Literal["mock", "cloud", "local"] = "mock"

    redis_url: str = "redis://127.0.0.1:6379/0"
    database_url: str = "postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw"
    vector_database_url: str | None = None

    run_timeout_ms: int = Field(15_000, gt=0)
    answer_token_budget: int = Field(400, gt=0)
    max_tool_calls: int = Field(4, gt=0)
    max_agent_handoffs: int = Field(2, ge=0)
    max_revisions: int = Field(1, ge=0)

    model_timeout_ms: int = Field(15_000, gt=0)
    tool_timeout_ms: int = Field(5_000, gt=0)
    sse_timeout_ms: int = Field(30_000, gt=0)

    cloud: CloudProviderConfig = Field(default_factory=CloudProviderConfig)
    local: LocalProviderConfig = Field(default_factory=LocalProviderConfig)

    def validate_for_environment(self) -> None:
        if self.environment in {"production", "staging"}:
            missing = []
            if not self.redis_url or any(
                h in self.redis_url for h in ("127.0.0.1", "localhost", "::1")
            ):
                missing.append(
                    "GCMW_REDIS_URL (production must not use localhost default)"
                )
            if not self.database_url or any(
                h in self.database_url for h in ("127.0.0.1", "localhost", "::1")
            ):
                missing.append(
                    "GCMW_DATABASE_URL (production must not use localhost default)"
                )
            if self.active_provider == "cloud" and not os.getenv(
                self.cloud.api_key_env
            ):
                missing.append(self.cloud.api_key_env)
            if (
                self.active_provider == "local"
                and self.local.api_key_env
                and not os.getenv(self.local.api_key_env)
            ):
                missing.append(self.local.api_key_env)
            if missing:
                raise RuntimeError(
                    f"Missing/invalid required configuration in {self.environment}: {missing}"
                )

    @classmethod
    def from_env(cls) -> Settings:
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer, got: {raw!r}") from exc

        def _bool(name: str, default: bool = False) -> bool:
            return os.getenv(name, "1" if default else "0") == "1"

        cloud = CloudProviderConfig(
            provider=os.getenv("GCMW_CLOUD_PROVIDER", "dashscope_realtime"),
            adapter=os.getenv("GCMW_CLOUD_ADAPTER", "cloud_realtime"),
            model=os.getenv("GCMW_CLOUD_MODEL", "qwen3.5-omni-plus-realtime"),
            api_base=os.getenv(
                "GCMW_CLOUD_API_BASE", "https://dashscope.aliyuncs.com/api/v1"
            ),
            api_key_env=os.getenv("GCMW_CLOUD_API_KEY_ENV", "GCMW_CLOUD_API_KEY"),
            transport="websocket",
            modalities=tuple(
                os.getenv("GCMW_CLOUD_MODALITIES", "text,audio,image,video").split(",")
            ),
            timeout_ms=_int("GCMW_CLOUD_TIMEOUT_MS", 15_000),
            connect_timeout_ms=_int("GCMW_CLOUD_CONNECT_TIMEOUT_MS", 5_000),
        )
        local = LocalProviderConfig(
            provider=os.getenv("GCMW_LOCAL_PROVIDER", "vllm"),
            adapter=os.getenv("GCMW_LOCAL_ADAPTER", "local_omni"),
            model=os.getenv("GCMW_LOCAL_MODEL", "Qwen/Qwen3-Omni-30B-A3B-Instruct"),
            api_base=os.getenv("GCMW_LOCAL_API_BASE", "http://127.0.0.1:8001/v1"),
            api_key_env=os.getenv("GCMW_LOCAL_API_KEY_ENV") or None,
            modalities=tuple(
                os.getenv("GCMW_LOCAL_MODALITIES", "text,audio,image,video").split(",")
            ),
            timeout_ms=_int("GCMW_LOCAL_TIMEOUT_MS", 30_000),
            connect_timeout_ms=_int("GCMW_LOCAL_CONNECT_TIMEOUT_MS", 5_000),
        )
        settings = cls(
            environment=os.getenv("GCMW_ENV", "development"),
            debug=_bool("GCMW_DEBUG"),
            active_provider=os.getenv("GCMW_ACTIVE_PROVIDER", "mock"),
            redis_url=os.getenv("GCMW_REDIS_URL", "redis://127.0.0.1:6379/0"),
            database_url=os.getenv(
                "GCMW_DATABASE_URL", "postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw"
            ),
            vector_database_url=os.getenv("GCMW_VECTOR_DATABASE_URL") or None,
            run_timeout_ms=_int("GCMW_RUN_TIMEOUT_MS", 15_000),
            answer_token_budget=_int("GCMW_ANSWER_TOKEN_BUDGET", 400),
            max_tool_calls=_int("GCMW_MAX_TOOL_CALLS", 4),
            max_agent_handoffs=_int("GCMW_MAX_AGENT_HANDOFFS", 2),
            max_revisions=_int("GCMW_MAX_REVISIONS", 1),
            model_timeout_ms=_int("GCMW_MODEL_TIMEOUT_MS", 15_000),
            tool_timeout_ms=_int("GCMW_TOOL_TIMEOUT_MS", 5_000),
            sse_timeout_ms=_int("GCMW_SSE_TIMEOUT_MS", 30_000),
            cloud=cloud,
            local=local,
        )
        settings.validate_for_environment()
        return settings
