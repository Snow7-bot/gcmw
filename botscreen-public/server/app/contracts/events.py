"""SSE and run event contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .run import RunState


class SSEEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(..., ge=1)
    run_id: str = Field(..., min_length=1, max_length=128)
    layer: str = Field(..., pattern="^(process|answer)$")
    event: str = Field(..., min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    state: RunState
    event_seq: int = Field(..., ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
