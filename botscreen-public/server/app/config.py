"""Application configuration with validation and secret references.

Secrets are never stored here. In production, missing required secret
references cause startup to fail.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

SUPPORTED_MODALITIES = ("text", "audio", "image", "video")


class CloudProviderConfig(BaseModel):
    provider: str = "dashscope_realtime"
    adapter: str = "cloud_realtime"
    model: str = "qwen3.5-omni-plus-realtime"
    api_base: str = "https://dashscope.aliyuncs.com/api/v1"
    api_key_env: str = "GCMW_CLOUD_API_KEY"
    transport: str = "websocket"
    modalities: tuple[str, ...] = ("text", "audio", "image", "video")
    timeout_ms: int = 15_000
    connect_timeout_ms: int = 5_000


class LocalProviderConfig(BaseModel):
    provider: str = "vllm"
    adapter: str = "local_omni"
    model: str = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    api_base: str = "http://127.0.0.1:8001/v1"
    api_key_env: Optional[str] = None
    modalities: tuple[str, ...] = ("text", "audio", "image", "video")
    timeout_ms: int = 30_000
    connect_timeout_ms: int = 5_000


class Settings(BaseModel):
    app_name: str = "gcmw-agent"
    environment: str = "development"
    debug: bool = False
    active_provider: str = "mock"

    redis_url: str = "redis://127.0.0.1:6379/0"
    database_url: str = "postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw"
    vector_database_url: Optional[str] = None

    run_timeout_ms: int = 15_000
    answer_token_budget: int = 400
    max_tool_calls: int = 4
    max_agent_handoffs: int = 2
    max_revisions: int = 1

    model_timeout_ms: int = 15_000
    tool_timeout_ms: int = 5_000
    sse_timeout_ms: int = 30_000

    cloud: CloudProviderConfig = Field(default_factory=CloudProviderConfig)
    local: LocalProviderConfig = Field(default_factory=LocalProviderConfig)

    def validate_for_environment(self) -> None:
        if self.environment in {"production", "staging"}:
            missing = []
            if self.active_provider == "cloud" and not os.getenv(self.cloud.api_key_env):
                missing.append(self.cloud.api_key_env)
            if self.active_provider == "local" and self.local.api_key_env and not os.getenv(self.local.api_key_env):
                missing.append(self.local.api_key_env)
            if missing:
                raise RuntimeError(
                    f"Missing required secret environment variables in {self.environment}: {missing}"
                )

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError:
                return default

        def _bool(name: str, default: bool = False) -> bool:
            return os.getenv(name, "1" if default else "0") == "1"

        cloud = CloudProviderConfig(
            provider=os.getenv("GCMW_CLOUD_PROVIDER", "dashscope_realtime"),
            adapter=os.getenv("GCMW_CLOUD_ADAPTER", "cloud_realtime"),
            model=os.getenv("GCMW_CLOUD_MODEL", "qwen3.5-omni-plus-realtime"),
            api_base=os.getenv("GCMW_CLOUD_API_BASE", "https://dashscope.aliyuncs.com/api/v1"),
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
            database_url=os.getenv("GCMW_DATABASE_URL", "postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw"),
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
