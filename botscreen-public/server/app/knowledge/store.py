"""Knowledge store with candidate/production zoning (issue #56A — tenancy & audit rework).

Reviewer-driven contract (2026-09 round):
- storage key is ``(tenant_id, source_id)``; the same ``source_id`` may exist
  independently in different tenants;
- every operation REQUIRES a trusted :class:`~app.contracts.common.TenantContext`
  (raw tenant/session strings are never accepted), so model-supplied payloads
  cannot widen or redirect a query — real trust is injected by the upstream
  authentication / ToolGateway boundary, this layer only consumes it;
- candidates enter through :class:`~app.contracts.knowledge.CandidateInput` —
  a content-only DTO. Lifecycle fields (review_status, reviewed_by/at,
  valid_from/to, knowledge_version, superseded_by, created_at) are produced by
  the store and can never be injected by upstream data;
- approvals take a strict :class:`ApprovalDecision` (AwareDatetime enforced by
  the contract) and new records are built through full ``model_validate`` so
  validation is never bypassed by ``model_copy(update=...)``;
- revocations take a :class:`RevocationDecision` (authenticated actor + reason
  + optional evidence reference) and append an immutable
  :class:`KnowledgeAuditEvent`; the revoked record KEEPS its version so the
  audit chain stays traceable;
- production view = approved + in-window + not superseded + complete approval
  metadata, tenant-scoped (the only view #53 RAG may query).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

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


def _payload(item: KnowledgeItem) -> dict:
    """Contract payload for revalidation: computed fields (content_hash) are
    derived, not input, so they must not be fed back into ``model_validate``."""
    return item.model_dump(exclude={"content_hash"})


def _key(context: TenantContext, source_id: str) -> Key:
    if not source_id or not source_id.strip():
        raise KnowledgeGovernanceError("source_id is required")
    return (_tenant(context), source_id)


class KnowledgeStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._items: dict[Key, KnowledgeItem] = {}  # (tenant, source_id) -> latest
        self._history: dict[Key, list[KnowledgeItem]] = {}
        self._audit: dict[Key, list[KnowledgeAuditEvent]] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- audit -----------------------------------------------------------------

    def _append_audit(
        self,
        key: Key,
        action: AuditAction,
        actor: str,
        *,
        knowledge_version: str | None = None,
        reason: str = "",
        evidence_ref: str = "",
    ) -> KnowledgeAuditEvent:
        """Append one immutable audit event (tenant+source scoped, no content
        body — identifiers, versions and the stated reason/evidence only)."""
        event = KnowledgeAuditEvent(
            at=self.now(),
            tenant_id=key[0],
            source_id=key[1],
            action=action,
            actor=actor or "system",
            knowledge_version=knowledge_version,
            reason=reason,
            evidence_ref=evidence_ref,
        )
        self._audit.setdefault(key, []).append(event)
        return event

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
        actor: str = "",
    ) -> KnowledgeItem:
        """Enter the candidate zone. The DTO carries content only; tenant and
        all lifecycle fields are produced here. Duplicate ``(tenant,
        source_id)`` is rejected until the previous item is revoked."""
        key = _key(context, candidate.source_id)
        with self._lock:
            if key in self._items:
                raise KnowledgeGovernanceError(
                    f"source {candidate.source_id!r} already exists for this tenant"
                )
            item = KnowledgeItem.model_validate(
                {
                    **candidate.model_dump(),
                    "tenant_id": key[0],
                    "review_status": ReviewStatus.DRAFT,
                }
            )
            self._items[key] = item.model_copy(deep=True)
            self._append_audit(key, AuditAction.CANDIDATE_ADDED, actor)
            return item

    def mark_in_review(
        self, context: TenantContext, source_id: str, *, actor: str = ""
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
            updated = KnowledgeItem.model_validate(
                {**_payload(item), "review_status": ReviewStatus.IN_REVIEW}
            )
            self._items[key] = updated.model_copy(deep=True)
            self._append_audit(key, AuditAction.IN_REVIEW, actor)
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
        """Clinical approval under an authenticated reviewer identity.

        ``decision`` carries aware datetimes (naive/ill-ordered windows are
        rejected by the contract BEFORE any state change) and the reviewer is
        injected upstream, never taken from model-controlled payloads. The new
        record is built through ``model_validate`` so every field is re-checked.
        """
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            prior = self._items[key]
            history = self._history.setdefault(key, [])
            if prior.review_status is ReviewStatus.APPROVED:
                # a re-approval supersedes the previous approved version, which
                # is moved to history (ordered supersede semantics)
                history.append(prior)
            approved_count = sum(
                1 for h in history if h.review_status is ReviewStatus.APPROVED
            )
            version_no = approved_count + 1
            version = f"{source_id}-v{version_no}"
            if prior.review_status is ReviewStatus.APPROVED:
                superseded = KnowledgeItem.model_validate(
                    {**_payload(prior), "superseded_by": version}
                )
                history[-1] = superseded
            approved = KnowledgeItem.model_validate(
                {
                    **_payload(item),
                    "review_status": ReviewStatus.APPROVED,
                    "reviewed_by": decision.reviewer,
                    "reviewed_at": self.now(),
                    "valid_from": decision.valid_from,
                    "valid_to": decision.valid_to,
                    "knowledge_version": version,
                    "superseded_by": None,
                }
            )
            self._items[key] = approved.model_copy(deep=True)
            self._append_audit(
                key,
                AuditAction.APPROVED,
                decision.reviewer,
                knowledge_version=version,
                evidence_ref=decision.evidence_ref,
            )
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
        """Revoke under an authenticated actor: the item leaves the production
        view immediately; the prior version and the revocation record (WITH its
        knowledge_version) plus an immutable audit event preserve the chain."""
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            revoked = KnowledgeItem.model_validate(
                {**_payload(item), "review_status": ReviewStatus.REVOKED}
            )
            history = self._history.setdefault(key, [])
            history.append(item.model_copy(deep=True))
            self._items.pop(key, None)
            history.append(revoked.model_copy(deep=True))
            self._append_audit(
                key,
                AuditAction.REVOKED,
                decision.actor,
                knowledge_version=item.knowledge_version,
                reason=decision.reason,
                evidence_ref=decision.evidence_ref,
            )
            return revoked

    def _require(self, key: Key) -> KnowledgeItem:
        item = self._items.get(key)
        if item is None:
            raise KnowledgeGovernanceError(
                f"source {key[1]!r} not found for this tenant"
            )
        return item
