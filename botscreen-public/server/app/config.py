"""Application configuration.

This module intentionally does not contain real secrets. All secret values are
injected through environment variables or a Secret Manager in production.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class CloudProviderConfig:
    provider: str = field(default_factory=lambda: _env("GCMW_CLOUD_PROVIDER", "dashscope_realtime"))
    adapter: str = field(default_factory=lambda: _env("GCMW_CLOUD_ADAPTER", "cloud_realtime"))
    model: str = field(default_factory=lambda: _env("GCMW_CLOUD_MODEL", "qwen3.5-omni-plus-realtime"))
    api_base: str = field(default_factory=lambda: _env("GCMW_CLOUD_API_BASE", "https://dashscope.aliyuncs.com/api/v1"))
    api_key_env: str = field(default_factory=lambda: _env("GCMW_CLOUD_API_KEY_ENV", "GCMW_CLOUD_API_KEY"))
    transport: str = "websocket"
    modalities: tuple[str, ...] = ("text", "audio", "image")


@dataclass(frozen=True)
class LocalProviderConfig:
    provider: str = field(default_factory=lambda: _env("GCMW_LOCAL_PROVIDER", "vllm"))
    adapter: str = field(default_factory=lambda: _env("GCMW_LOCAL_ADAPTER", "local_omni"))
    model: str = field(default_factory=lambda: _env("GCMW_LOCAL_MODEL", "Qwen/Qwen3-Omni-30B-A3B-Instruct"))
    api_base: str = field(default_factory=lambda: _env("GCMW_LOCAL_API_BASE", "http://127.0.0.1:8001/v1"))
    api_key: Optional[str] = None
    modalities: tuple[str, ...] = ("text", "audio", "image")


@dataclass(frozen=True)
class Settings:
    app_name: str = "gcmw-agent"
    environment: str = field(default_factory=lambda: _env("GCMW_ENV", "development"))
    debug: bool = field(default_factory=lambda: _env("GCMW_DEBUG", "0") == "1")
    active_provider: str = field(default_factory=lambda: _env("GCMW_ACTIVE_PROVIDER", "mock"))

    redis_url: str = field(default_factory=lambda: _env("GCMW_REDIS_URL", "redis://127.0.0.1:6379/0"))
    database_url: str = field(default_factory=lambda: _env("GCMW_DATABASE_URL", "postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw"))
    vector_database_url: str = field(default_factory=lambda: _env("GCMW_VECTOR_DATABASE_URL", ""))

    run_timeout_ms: int = field(default_factory=lambda: int(_env("GCMW_RUN_TIMEOUT_MS", "15000")))
    answer_token_budget: int = field(default_factory=lambda: int(_env("GCMW_ANSWER_TOKEN_BUDGET", "400")))
    max_tool_calls: int = field(default_factory=lambda: int(_env("GCMW_MAX_TOOL_CALLS", "4")))
    max_agent_handoffs: int = field(default_factory=lambda: int(_env("GCMW_MAX_AGENT_HANDOFFS", "2")))
    max_revisions: int = field(default_factory=lambda: int(_env("GCMW_MAX_REVISIONS", "1")))

    cloud: CloudProviderConfig = field(default_factory=CloudProviderConfig)
    local: LocalProviderConfig = field(default_factory=LocalProviderConfig)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()


settings = Settings.from_env()
