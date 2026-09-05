"""Audit contract for traceable actions."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tenant_id: str = Field(..., min_length=1, max_length=64)
    actor_type: str = Field(..., min_length=1, max_length=32)
    actor_id_hash: str = Field(..., min_length=1, max_length=128)
    session_id_hash: str = ""
    request_id: str = Field(..., min_length=1, max_length=128)
    run_id: str = ""
    action: str = Field(..., min_length=1, max_length=64)
    agent_ids: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    provider_id: str = ""
    model_id: str = ""
    model_version: str = ""
    knowledge_version: str = ""
    prompt_version: str = ""
    policy_version: str = ""
    safety_decision: str = ""
    citation_ids: list[str] = Field(default_factory=list)
    answer_hash: str = ""
    result: str = ""
    error_code: str = ""
    latency_ms: int = 0
