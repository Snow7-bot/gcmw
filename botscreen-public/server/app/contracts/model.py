"""ModelGateway request/response and content part contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, Field


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


class ContentPart(BaseModel):
    type: ContentType
    text: Optional[str] = None
    media_ref: Optional[str] = None
    mime_type: Optional[str] = None


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    content_parts: list[ContentPart] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    response_schema: Optional[dict[str, Any]] = None
    stream: bool = False
    deadline_ms: int = 10_000
    token_budget: int = 400
    trace_id: str = ""
    provider_hint: Optional[str] = None


class ModelResponse(BaseModel):
    provider_id: str
    model_id: str
    model_version: str = ""
    content: str = ""
    content_parts: list[ContentPart] = Field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    error_code: Optional[str] = None


class ModelEvent(BaseModel):
    type: str  # e.g. delta, done, error, tool_call
    provider_id: str
    model_id: str
    model_version: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
