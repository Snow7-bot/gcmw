"""Agent API request/response contracts (issue #36, API surface v1).

Notes:
- v1 input is text-only; voice/image content parts arrive with the
  multimodal work (#51) under the same envelope shape;
- ``Channel`` comes from contracts.common and is a SESSION property — runs
  inherit the session channel and never carry their own;
- tenant/device identities are NOT accepted here: they derive from the
  authenticated DevicePrincipal (see api/v1/auth.py); full credential
  validation lands in #36c.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .common import Channel
from .run import RunState


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Channel = Channel.TEXT
    locale: str = Field("zh-CN", min_length=2, max_length=16)


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=128)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=128)
    channel: Channel
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    ttl_s: int = Field(1800, ge=1)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field("text", pattern="^text$")  # v1: text only
    text: str = Field(..., min_length=1, max_length=4000)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=128)
    input: RunInput
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class RunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    session_id: str = Field(..., min_length=1, max_length=128)
    state: RunState = RunState.ACCEPTED
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    cancelled: bool = False
