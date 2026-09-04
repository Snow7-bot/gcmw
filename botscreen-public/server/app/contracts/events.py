"""SSE and run event contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .run import RunState


class SSEEvent(BaseModel):
    seq: int
    run_id: str
    layer: str = Field(..., pattern="^(process|answer)$")
    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RunEvent(BaseModel):
    run_id: str
    state: RunState
    event_seq: int
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
