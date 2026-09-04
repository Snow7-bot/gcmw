"""Agent manifest, context and result contracts."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentManifest(BaseModel):
    agent_id: str
    version: str
    supported_intents: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    required_data_scopes: list[str] = Field(default_factory=list)
    default_timeout_ms: int = 10_000
    risk_level: RiskLevel = RiskLevel.LOW
    enabled: bool = True


class AgentContext(BaseModel):
    tenant_id: str
    device_id: str
    session_id: str
    run_id: str
    channel: str
    normalized_input: str = ""
    short_memory_summary: str = ""
    permitted_long_memory: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    deadline: Optional[datetime] = None


class ToolRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    deadline: Optional[datetime] = None


class ToolResult(BaseModel):
    tool_name: str
    ok: bool = True
    data: Any = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class Evidence(BaseModel):
    source_id: str
    source_type: str
    title: str = ""
    content: str = ""
    source_uri: str = ""
    content_hash: str = ""
    knowledge_version: str = ""


class AgentResult(BaseModel):
    agent_id: str
    status: AgentStatus
    answer_candidate: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    confidence_band: Optional[str] = None
    safety_status: str = "unknown"
    memory_candidates: list[dict[str, Any]] = Field(default_factory=list)
    public_trace: list[dict[str, Any]] = Field(default_factory=list)
