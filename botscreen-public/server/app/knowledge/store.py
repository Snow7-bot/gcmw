"""Knowledge store with candidate/production zoning (issue #56A — tenancy rework).

Reviewer-driven tenancy contract (2026-09 round):
- the storage key is ``(tenant_id, source_id)`` — the same ``source_id`` may
  exist independently in different tenants (no global key collisions, no
  cross-tenant supersede/version bleed);
- every operation REQUIRES a trusted :class:`~app.contracts.common.TenantContext`
  as its first argument; raw ``tenant_id`` strings are never accepted from
  callers, so a model-supplied parameter can never widen or redirect a query
  (the #57 tool layer is re-based onto this contract after #68 merges);
- ``add_candidate`` refuses an item whose ``tenant_id`` differs from the
  trusted context (defense in depth: the context is the authority);
- lookups are tenant-scoped: another tenant's ``source_id`` reads as absent.

Existing governance semantics are unchanged:
- every item enters as a candidate (draft/in_review);
- only APPROVED + in-window items of the calling tenant are visible through
  the production view (the only view #53 RAG is allowed to query);
- approvals are versioned per tenant; re-approval supersedes the previous
  approved version (kept in history); revoke removes an item from the
  production view but keeps the audit record;
- ``content_hash`` is derived from content (sha256); no raw fingerprints or
  question text are stored;
- the clock is injectable so validity-window expiry is testable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from app.contracts.common import TenantContext
from app.contracts.knowledge import KnowledgeItem, ReviewStatus

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


class KnowledgeStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._items: dict[Key, KnowledgeItem] = {}  # (tenant, source_id) -> latest
        self._history: dict[Key, list[KnowledgeItem]] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- candidate zone --------------------------------------------------------

    def add_candidate(
        self, context: TenantContext, item: KnowledgeItem
    ) -> KnowledgeItem:
        """Enter the candidate zone under the trusted tenant.

        Duplicate ``(tenant, source_id)`` is rejected until the previous item
        is revoked; an item whose ``tenant_id`` contradicts the context is
        refused outright.
        """
        key = _key(context, item.source_id)
        with self._lock:
            if item.tenant_id != key[0]:
                raise KnowledgeGovernanceError(
                    "item tenant_id does not match the trusted tenant context"
                )
            if key in self._items:
                raise KnowledgeGovernanceError(
                    f"source {item.source_id!r} already exists for this tenant"
                )
            if item.review_status not in (ReviewStatus.DRAFT, ReviewStatus.IN_REVIEW):
                raise KnowledgeGovernanceError(
                    "new items must start as draft/in_review candidates"
                )
            stored = item.model_copy(deep=True)
            # internal state and the returned snapshot are separate copies:
            # caller mutation can never corrupt store state (or vice versa)
            self._items[key] = stored.model_copy(deep=True)
            return stored

    def mark_in_review(self, context: TenantContext, source_id: str) -> KnowledgeItem:
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            if item.review_status is ReviewStatus.APPROVED:
                # approved items must go through revoke before re-review, so the
                # production view never loses an approved item without an audit
                raise KnowledgeGovernanceError(
                    f"source {source_id!r} is APPROVED — revoke before re-review"
                )
            updated = item.model_copy(
                deep=True, update={"review_status": ReviewStatus.IN_REVIEW}
            )
            self._items[key] = updated.model_copy(deep=True)
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
        reviewer: str,
        *,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> KnowledgeItem:
        """Clinical approval within the calling tenant. Produces the next
        immutable knowledge_version; any previous approved version of the same
        ``(tenant, source_id)`` is superseded (kept in history)."""
        if not reviewer or not reviewer.strip():
            raise KnowledgeGovernanceError("reviewer is required")
        if valid_from is not None and valid_to is not None and valid_from >= valid_to:
            raise KnowledgeGovernanceError("valid_from must precede valid_to")
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
            if prior.review_status is ReviewStatus.APPROVED:
                # record which version superseded the moved entry
                superseded = prior.model_copy(
                    deep=True,
                    update={"superseded_by": f"{source_id}-v{version_no}"},
                )
                history[-1] = superseded
            approved = item.model_copy(
                deep=True,
                update={
                    "review_status": ReviewStatus.APPROVED,
                    "reviewed_by": reviewer,
                    "reviewed_at": self.now(),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "knowledge_version": f"{source_id}-v{version_no}",
                },
            )
            self._items[key] = approved.model_copy(deep=True)
            return approved

    # -- production view ---------------------------------------------------------

    def production_items(self, context: TenantContext) -> list[KnowledgeItem]:
        """APPROVED + in-window items of the calling tenant. This is the only
        view #53 RAG may query."""
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
        self, context: TenantContext, source_id: str, reason: str
    ) -> KnowledgeItem:
        """Remove the tenant's item from the production view immediately; it
        stays as an auditable revoked record and the source_id can be
        re-entered within the same tenant."""
        if not reason or not reason.strip():
            raise KnowledgeGovernanceError("revocation reason is required")
        key = _key(context, source_id)
        with self._lock:
            item = self._require(key)
            revoked = item.model_copy(
                deep=True,
                update={
                    "review_status": ReviewStatus.REVOKED,
                    "knowledge_version": None,
                },
            )
            history = self._history.setdefault(key, [])
            history.append(item.model_copy(deep=True))
            self._items.pop(key, None)
            history.append(revoked.model_copy(deep=True))
            return revoked

    def _require(self, key: Key) -> KnowledgeItem:
        item = self._items.get(key)
        if item is None:
            raise KnowledgeGovernanceError(
                f"source {key[1]!r} not found for this tenant"
            )
        return item
