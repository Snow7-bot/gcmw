"""ModelGateway request/response and content part contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .errors import ErrorCode


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


class ModelEventType(str, Enum):
    DELTA = "delta"
    DONE = "done"
    ERROR = "error"
    TOOL_CALL = "tool_call"


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ContentType
    text: str | None = None
    media_ref: str | None = None
    mime_type: str | None = None

    @field_validator("media_ref")
    @classmethod
    def media_ref_required_for_non_text(cls, v, info):
        if info.data.get("type") != ContentType.TEXT and not v:
            raise ValueError("media_ref is required for non-text content parts")
        return v

    @model_validator(mode="after")
    def validate_non_text_media_ref(self):
        if self.type != ContentType.TEXT and not self.media_ref:
            raise ValueError("media_ref is required for non-text content parts")
        if self.type == ContentType.TEXT and not (self.text or "").strip():
            raise ValueError("text is required for text content parts")
        return self


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(default_factory=list)
    content_parts: list[ContentPart] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    safety_context: dict[str, Any] = Field(default_factory=dict)
    stream: bool = False
    deadline_ms: int = Field(10000, gt=0)
    token_budget: int = Field(400, ge=1)
    trace_id: str = Field(..., min_length=1, max_length=128)
    provider_hint: str | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=64)
    content: str = ""
    content_parts: list[ContentPart] = Field(default_factory=list)
    input_modalities: list[ContentType] = Field(default_factory=list)
    recognition_text: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    error_code: ErrorCode | None = None


class ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ModelEventType
    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ModelGateway types (issue #37). Provider adapters and health/schema
# reporting share these contracts; Agent code only ever talks to ModelGateway.
# ---------------------------------------------------------------------------


class ProviderStatus(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., min_length=1, max_length=64)
    status: ProviderStatus
    latency_ms: int = Field(0, ge=0)
    checked_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # safe, generic message only — never raw provider output
    message: str = ""


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=64)
    supported_modalities: list[ContentType] = Field(default_factory=list)
    supports_function_calling: bool = False
    supports_streaming: bool = False
    supports_realtime: bool = False
    supports_json_schema: bool = False
    # locked snapshot / alias policy (e.g. "2026-03-15" pinned snapshot)
    snapshot: str = ""


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_provider_id: str = Field(..., min_length=1, max_length=64)
    release_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=256)


class SwitchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    previous_provider_id: str
    active_provider_id: str
    release_id: str
    switched_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    error_code: ErrorCode | None = None


class RealtimeSessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=128)
    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    opened_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Never carries the provider URL, API key or long-lived tokens: browsers and
    # ROS endpoints must not be able to reach the vendor directly (V2.3 §8.2).
