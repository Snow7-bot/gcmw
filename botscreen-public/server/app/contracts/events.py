"""SSE and run event contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .run import RunState


class SSEEventType(str, Enum):
    RUN_ACCEPTED = "run.accepted"
    PROCESS_STATUS = "process.status"
    EVIDENCE_FOUND = "evidence.found"
    REFLECTION_RESULT = "reflection.result"
    ANSWER_DELTA = "answer.delta"
    ANSWER_COMPLETED = "answer.completed"
    RUN_COMPLETED = "run.completed"
    MIC_STATUS = "mic_status"


# Extension strategy: add new members with explicit version bumps in protocol_version.


class SSEEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = "1.0"
    seq: int = Field(..., ge=1)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=128)
    session_id: str = Field(..., min_length=1, max_length=128)
    run_id: str = Field(..., min_length=1, max_length=128)
    layer: Literal["process", "answer"]
    event: SSEEventType
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    state: RunState
    event_seq: int = Field(..., ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
