"""Knowledge governance contracts (issue #56).

Every medical content item starts in the CANDIDATE zone. Only items that are
APPROVED, inside their validity window and tenant-matching may enter the
production index (queried by #53 RAG). Raw question text and fingerprints
never touch this store; content_hash is the sha256 of ``content``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

#: shared identity/actor type — trimmed and non-empty, so whitespace-only
#: reviewers/actors/reasons can never pass validation
NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
#: free-text reason (revocation etc.) — trimmed and non-empty
ReasonStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]


def _require_utc(value: datetime | None) -> datetime | None:
    """Lifecycle timestamps must be tz-aware UTC (single source of truth)."""
    if value is None:
        return None
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


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
    reviewed_by: NonEmptyStr | None = None
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
    """Strict, frozen approval input.

    - reviewer is a trimmed non-empty authenticated identity;
    - NO operation timestamp is accepted here: audit time is produced by the
      store's trusted server clock (``extra="forbid"`` rejects an ``at``
      field), so a caller can neither backdate nor postdate an approval;
    - the clinical validity window must be aware UTC (contract-enforced).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer: NonEmptyStr
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    evidence_ref: str = Field("", max_length=256)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _utc_only(cls, value):
        return _require_utc(value)

    @model_validator(mode="after")
    def _window_ordered(self) -> ApprovalDecision:
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("valid_from must precede valid_to")
        return self


class RevocationDecision(BaseModel):
    """Strict, frozen revocation input: trimmed non-empty actor + reason and
    an optional evidence reference.

    Like approvals, revocation carries NO timestamp — the revocation time is
    produced by the store's trusted server clock, so audit time cannot be
    backdated by the caller (``extra="forbid"`` rejects ``at``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: NonEmptyStr
    reason: ReasonStr
    evidence_ref: str = Field("", max_length=256)


class AuditAction(str, Enum):
    CANDIDATE_ADDED = "candidate.added"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REVOKED = "revoked"


class KnowledgeAuditEvent(BaseModel):
    """Frozen (truly immutable) audit event, append-only and tenant-scoped.

    Records actor/action/tenant/source/version/time plus the reason or
    evidence reference — never the full content body. ``actor`` is a trimmed
    non-empty identity; ``reason`` carries the revocation reason when present.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tenant_id: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=128)
    action: AuditAction
    actor: NonEmptyStr
    knowledge_version: str | None = None
    reason: ReasonStr | None = None
    evidence_ref: str = Field("", max_length=256)

    @field_validator("at")
    @classmethod
    def _utc_only(cls, value):
        return _require_utc(value)
