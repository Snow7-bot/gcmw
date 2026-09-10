"""Knowledge store with candidate/production zoning (issue #56A — tenancy, audit & publish-integrity rework).

Reviewer-driven contract (2026-09 round 3):
- storage key is ``(tenant_id, source_id)``; tenancy always comes from a
  trusted :class:`~app.contracts.common.TenantContext` (raw tenant/session
  strings are never accepted — real trust is injected upstream by the
  authentication / ToolGateway boundary, this layer only consumes it);
- candidates enter through the content-only :class:`CandidateInput` DTO;
  lifecycle fields are produced here and can never be submitted;
- every mutation is an ATOMIC COMMIT: records, history and the audit event are
  fully constructed and validated FIRST, then the in-memory structures are
  updated together — a rejected audit/actor can never leave a state change
  behind;
- publishing is single-transition: only ``IN_REVIEW -> APPROVED`` succeeds.
  Repeated or concurrent approvals conflict (no v2..v10 from replays); a
  republication must go revoke -> new candidate -> in_review -> approve;
- approvals/revocations carry the authenticated actor and ONE validated UTC
  operation time used for both the record and its audit event;
- revocations keep the revoked record's ``knowledge_version`` and append an
  immutable audit event (actor/action/tenant/source/version/time/reason or
  evidence reference — never the content body);
- production view = approved + in-window + not superseded + complete approval
  metadata, tenant-scoped (the only view #53 RAG may query).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.contracts.common import TenantContext
from app.contracts.knowledge import (
    ApprovalDecision,
    AuditAction,
    CandidateInput,
    KnowledgeAuditEvent,
    KnowledgeItem,
    ReviewStatus,
    RevocationDecision,
)

#: storage key — tenancy is part of the identity, never a filter
Key = tuple[str, str]


class KnowledgeGovernanceError(RuntimeError):
    """Knowledge store failure (contract mapping in #36 error boundary)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _tenant(context: TenantContext) -> str:
    """Trusted tenant id extraction — the only source of tenancy."""
    if context is None or not getattr(context, "tenant_id", ""):
        raise KnowledgeGovernanceError("trusted tenant context is required")
    return context.tenant_id


def _key(context: TenantContext, source_id: str) -> Key:
    if not source_id or not source_id.strip():
        raise KnowledgeGovernanceError("source_id is required")
    return (_tenant(context), source_id)


def _payload(item: KnowledgeItem) -> dict:
    """Contract payload for revalidation: computed fields (content_hash) are
    derived, not input, so they must not be fed back into ``model_validate``."""
    return item.model_dump(exclude={"content_hash"})


class KnowledgeStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._items: dict[Key, KnowledgeItem] = {}  # (tenant, source_id) -> latest
        self._history: dict[Key, list[KnowledgeItem]] = {}
        self._audit: dict[Key, list[KnowledgeAuditEvent]] = {}

    def now(self) -> datetime:
        return self._clock()

    def _now_utc(self) -> datetime:
        """Trusted operation time: read the injected clock ONCE and require a
        tz-aware UTC instant. A naive or non-UTC clock raises BEFORE any state,
        history or audit write — audit time can never be caller-supplied."""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise KnowledgeGovernanceError(
                "server clock must return a timezone-aware UTC datetime"
            )
        return value

    # -- audit -----------------------------------------------------------------

    def _build_audit(
        self,
        key: Key,
        action: AuditAction,
        actor: str,
        *,
        at: datetime,
        knowledge_version: str | None = None,
        reason: str | None = None,
        evidence_ref: str = "",
    ) -> KnowledgeAuditEvent:
        """Construct and validate one audit event (no mutation). Actor is an
        explicit service identity — there is no implicit default."""
        return KnowledgeAuditEvent(
            at=at,
            tenant_id=key[0],
            source_id=key[1],
            action=action,
            actor=actor,
            knowledge_version=knowledge_version,
            reason=reason,
            evidence_ref=evidence_ref,
        )

    def audit_trail(
        self, context: TenantContext, source_id: str
    ) -> list[KnowledgeAuditEvent]:
        """Immutable audit history of one item, tenant-scoped."""
        key = _key(context, source_id)
        with self._lock:
            if (
                key not in self._audit
                and key not in self._items
                and key not in self._history
            ):
                # unknown to this tenant => absent (never another tenant's data)
                raise KnowledgeGovernanceError(
                    f"source {source_id!r} not found for this tenant"
                )
            return [e.model_copy(deep=True) for e in self._audit.get(key, [])]

    # -- candidate zone --------------------------------------------------------

    def add_candidate(
        self,
        context: TenantContext,
        candidate: CandidateInput,
        *,
        actor: str,
    ) -> KnowledgeItem:
        """Enter the candidate zone. Record + audit event are built first and
        committed together; a rejected audit leaves the store untouched."""
        key = _key(context, candidate.source_id)
        with self._lock:
            if key in self._items:
                raise KnowledgeGovernanceError(
                    f"source {candidate.source_id!r} already exists for this tenant"
                )
            operation_at = self._now_utc()
            item = KnowledgeItem.model_validate(
                {
                    **candidate.model_dump(),
                    "tenant_id": key[0],
                    "review_status": ReviewStatus.DRAFT,
                    "created_at": operation_at,
                }
            )
            event = self._build_audit(
                key, AuditAction.CANDIDATE_ADDED, actor, at=operation_at
            )
            # -- atomic commit --
            self._items[key] = item.model_copy(deep=True)
            self._audit.setdefault(key, []).append(event)
            return item

    def mark_in_review(
        self, context: TenantContext, source_id: str, *, actor: str
    ) -> KnowledgeItem:
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            if item.review_status is ReviewStatus.APPROVED:
                # approved items must go through revoke before re-review, so the
                # production view never loses an approved item without an audit
                raise KnowledgeGovernanceError(
                    f"source {source_id!r} is APPROVED — revoke before re-review"
                )
            if item.review_status is ReviewStatus.IN_REVIEW:
                # already in review: conflict, and never a duplicate audit event
                raise KnowledgeGovernanceError(
                    f"source {source_id!r} is already IN_REVIEW"
                )
            operation_at = self._now_utc()
            updated = KnowledgeItem.model_validate(
                {**_payload(item), "review_status": ReviewStatus.IN_REVIEW}
            )
            event = self._build_audit(
                key, AuditAction.IN_REVIEW, actor, at=operation_at
            )
            # -- atomic commit --
            self._items[key] = updated.model_copy(deep=True)
            self._audit.setdefault(key, []).append(event)
            return updated

    def list_candidates(self, context: TenantContext) -> list[KnowledgeItem]:
        """Candidates of the calling tenant only (never another tenant's)."""
        tenant_id = _tenant(context)
        with self._lock:
            return [
                item.model_copy(deep=True)
                for (t, _), item in self._items.items()
                if t == tenant_id and item.review_status is not ReviewStatus.APPROVED
            ]

    # -- approval / publication -------------------------------------------------

    def approve(
        self,
        context: TenantContext,
        source_id: str,
        decision: ApprovalDecision,
    ) -> KnowledgeItem:
        """Single-transition publish: ``IN_REVIEW -> APPROVED`` only.

        Any other state (draft, already approved, revoked) conflicts, so
        replays and concurrent approvals can never mint extra versions. The
        record and its audit event share ``decision.at`` (one UTC instant) and
        are committed together.
        """
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            if item.review_status is not ReviewStatus.IN_REVIEW:
                raise KnowledgeGovernanceError(
                    f"source {source_id!r} is {item.review_status.value} — "
                    "only IN_REVIEW items can be approved"
                )
            history = self._history.get(key, [])
            approved_count = sum(
                1 for h in history if h.review_status is ReviewStatus.APPROVED
            )
            version = f"{source_id}-v{approved_count + 1}"
            operation_at = self._now_utc()  # trusted: never caller-supplied
            approved = KnowledgeItem.model_validate(
                {
                    **_payload(item),
                    "review_status": ReviewStatus.APPROVED,
                    "reviewed_by": decision.reviewer,
                    "reviewed_at": operation_at,
                    "valid_from": decision.valid_from,
                    "valid_to": decision.valid_to,
                    "knowledge_version": version,
                    "superseded_by": None,
                }
            )
            event = self._build_audit(
                key,
                AuditAction.APPROVED,
                decision.reviewer,
                at=operation_at,
                knowledge_version=version,
                evidence_ref=decision.evidence_ref,
            )
            # -- atomic commit --
            self._items[key] = approved.model_copy(deep=True)
            self._audit.setdefault(key, []).append(event)
            return approved

    # -- production view ---------------------------------------------------------

    def production_items(self, context: TenantContext) -> list[KnowledgeItem]:
        """APPROVED + in-window + not superseded + complete approval metadata,
        for the calling tenant only. This is the only view #53 RAG may query."""
        tenant_id = _tenant(context)
        with self._lock:
            return [
                item.model_copy(deep=True)
                for (t, _), item in self._items.items()
                if t == tenant_id and item.is_production_ready(self.now())
            ]

    def get(self, context: TenantContext, source_id: str) -> KnowledgeItem:
        key = _key(context, source_id)
        with self._lock:
            return self._require(key).model_copy(deep=True)

    def history(self, context: TenantContext, source_id: str) -> list[KnowledgeItem]:
        key = _key(context, source_id)
        with self._lock:
            history = list(self._history.get(key, []))
            current = self._items.get(key)
            if current is not None:
                history.append(current)
            return [item.model_copy(deep=True) for item in history]

    def revoke(
        self,
        context: TenantContext,
        source_id: str,
        decision: RevocationDecision,
    ) -> KnowledgeItem:
        """Revoke under an authenticated actor: the item leaves production
        immediately; the prior version and the revocation record (WITH its
        ``knowledge_version``) plus an immutable audit event preserve the
        chain. Record, history entries and audit event commit together."""
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            operation_at = self._now_utc()
            revoked = KnowledgeItem.model_validate(
                {**_payload(item), "review_status": ReviewStatus.REVOKED}
            )
            event = self._build_audit(
                key,
                AuditAction.REVOKED,
                decision.actor,
                at=operation_at,
                knowledge_version=item.knowledge_version,
                reason=decision.reason,
                evidence_ref=decision.evidence_ref,
            )
            # -- atomic commit --
            history = self._history.setdefault(key, [])
            history.append(item.model_copy(deep=True))
            self._items.pop(key, None)
            history.append(revoked.model_copy(deep=True))
            self._audit.setdefault(key, []).append(event)
            return revoked

    def _require(self, key: Key) -> KnowledgeItem:
        item = self._items.get(key)
        if item is None:
            raise KnowledgeGovernanceError(
                f"source {key[1]!r} not found for this tenant"
            )
        return item
