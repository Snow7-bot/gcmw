"""Common context and identity contracts."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Channel(str, Enum):
    TOUCH = "touch"
    TEXT = "text"
    VOICE = "voice"


class TenantContext(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64)


class DeviceContext(BaseModel):
    tenant_id: str
    device_id: str


class SessionContext(BaseModel):
    tenant_id: str
    device_id: str
    session_id: str


class RunContext(BaseModel):
    tenant_id: str
    device_id: str
    session_id: str
    run_id: str
    request_id: str
    idempotency_key: str
    channel: Channel
    created_at: datetime = Field(default_factory=datetime.utcnow)
    locale: str = "zh-CN"
