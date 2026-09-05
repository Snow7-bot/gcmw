"""ModelGateway request/response and content part contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


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
    finish_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    error_code: str | None = None


class ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1, max_length=32)
    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
