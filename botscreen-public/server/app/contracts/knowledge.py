"""Knowledge governance contracts (issue #56).

Every medical content item starts in the CANDIDATE zone. Only items that are
APPROVED, inside their validity window and tenant-matching may enter the
production index (queried by #53 RAG). Raw question text and fingerprints
never touch this store; content_hash is the sha256 of ``content``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)


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
        """Approved, not superseded/revoked, inside its validity window and
        carrying complete approval metadata.

        A record without ``knowledge_version`` / ``reviewed_by`` /
        ``reviewed_at`` is never production-ready — lifecycle fields must come
        from the system, so their absence means the record was never properly
        published. ``superseded_by`` must be None: the predicate mirrors the
        documented supersede semantics instead of trusting call sites.
        """
        now = at or datetime.now(timezone.utc)
        return (
            self.review_status is ReviewStatus.APPROVED
            and self.superseded_by is None
            and self.knowledge_version is not None
            and bool(self.reviewed_by)
            and self.reviewed_at is not None
            and (self.valid_from is None or now >= self.valid_from)
            and (self.valid_to is None or now < self.valid_to)
        )


# ---------------------------------------------------------------------------
# System-controlled inputs (issue #56A review round): candidates and review
# decisions are separate contracts from the persisted record, so lifecycle
# fields can never be injected by upstream data.
# ---------------------------------------------------------------------------


class CandidateInput(BaseModel):
    """What a content owner may submit: content fields ONLY.

    Every lifecycle/system field (``review_status``, ``reviewed_by``,
    ``reviewed_at``, ``valid_from/to``, ``knowledge_version``,
    ``superseded_by``, ``created_at``) is absent and therefore unforgeable —
    ``extra="forbid"`` rejects any attempt to smuggle one in. Tenancy is taken
    from the trusted context, never from this payload.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=128)
    source_type: KnowledgeSourceType
    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)
    source_uri: str = Field(..., min_length=1, max_length=1024)
    medical_domain: str = Field("general", min_length=1, max_length=64)
    risk_level: MedicalRisk = MedicalRisk.LOW
    audience: str = Field("public", min_length=1, max_length=64)


class ApprovalDecision(BaseModel):
    """Strict approval input: aware datetimes are enforced by the contract, so
    a naive/ill-formed window can never reach the store."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(..., min_length=1, max_length=128)
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    evidence_ref: str = Field("", max_length=256)

    @model_validator(mode="after")
    def _window_ordered(self) -> ApprovalDecision:
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("valid_from must precede valid_to")
        return self


class RevocationDecision(BaseModel):
    """Strict revocation input: reason + optional evidence reference, always
    paired with the authenticated actor injected by the caller layer."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    evidence_ref: str = Field("", max_length=256)


class AuditAction(str, Enum):
    CANDIDATE_ADDED = "candidate.added"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REVOKED = "revoked"


class KnowledgeAuditEvent(BaseModel):
    """Immutable audit event (append-only, tenant-scoped).

    Records actor/action/tenant/source/version/time plus the reason or
    evidence reference — never the full content body.
    """

    model_config = ConfigDict(extra="forbid")

    at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tenant_id: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=128)
    action: AuditAction
    actor: str = Field(..., min_length=1, max_length=128)
    knowledge_version: str | None = None
    reason: str = Field("", max_length=512)
    evidence_ref: str = Field("", max_length=256)
