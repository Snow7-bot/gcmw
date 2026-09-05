"""ModelGateway request/response and content part contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ContentType
    text: Optional[str] = None
    media_ref: Optional[str] = None
    mime_type: Optional[str] = None


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
    response_schema: Optional[dict[str, Any]] = None
    stream: bool = False
    deadline_ms: int = Field(10000, gt=0)
    token_budget: int = Field(400, ge=1)
    trace_id: str = Field(..., min_length=1, max_length=128)
    provider_hint: Optional[str] = None


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
    error_code: Optional[str] = None


class ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1, max_length=32)
    provider_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
