"""Knowledge governance contracts (issue #56).

Every medical content item starts in the CANDIDATE zone. Only items that are
APPROVED, inside their validity window and tenant-matching may enter the
production index (queried by #53 RAG). Raw question text and fingerprints
never touch this store; content_hash is the sha256 of ``content``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field


class KnowledgeSourceType(str, Enum):
    FAQ = "faq"
    DEPARTMENT = "department"
    STAFF = "staff"
    VIDEO = "video"
    DOCUMENT = "document"


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REVOKED = "revoked"


class MedicalRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class KnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=128)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    source_type: KnowledgeSourceType
    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)
    source_uri: str = Field(..., min_length=1, max_length=1024)
    medical_domain: str = Field("general", min_length=1, max_length=64)
    risk_level: MedicalRisk = MedicalRisk.LOW
    audience: str = Field("public", min_length=1, max_length=64)

    # review & lifecycle
    review_status: ReviewStatus = ReviewStatus.DRAFT
    reviewed_by: str | None = None
    reviewed_at: AwareDatetime | None = None
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    knowledge_version: str | None = None  # set on approval: "<source_id>-v<N>"
    superseded_by: str | None = None

    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        import hashlib

        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def is_production_ready(self, at: datetime | None = None) -> bool:
        """Approved, in its validity window, not revoked/superseded."""
        now = at or datetime.now(timezone.utc)
        return (
            self.review_status is ReviewStatus.APPROVED
            and (self.valid_from is None or now >= self.valid_from)
            and (self.valid_to is None or now < self.valid_to)
        )
