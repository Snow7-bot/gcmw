"""Knowledge store with candidate/production zoning (issue #56).

- every item enters as a candidate (draft/in_review);
- only APPROVED + in-window + tenant-matching items are visible through the
  production view (the only view #53 RAG is allowed to query);
- approvals are versioned: re-approving an item supersedes the previous
  approved version (kept in history); revoke removes an item from the
  production view but keeps the audit record;
- content_hash is derived from content (sha256) and validated at add-time by
  the contract itself; this store keeps no raw fingerprints or question text;
- clock is injectable so validity-window expiry is testable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from app.contracts.knowledge import KnowledgeItem, ReviewStatus


class KnowledgeGovernanceError(RuntimeError):
    """Knowledge store failure (contract mapping in #36 error boundary)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class KnowledgeStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._items: dict[str, KnowledgeItem] = {}  # source_id -> latest
        self._history: dict[str, list[KnowledgeItem]] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- candidate zone --------------------------------------------------------

    def add_candidate(self, item: KnowledgeItem) -> KnowledgeItem:
        """Enter the candidate zone. Duplicate source_id is rejected until the
        previous item is revoked."""
        with self._lock:
            if item.source_id in self._items:
                raise KnowledgeGovernanceError(
                    f"source {item.source_id!r} already exists"
                )
            if item.review_status not in (ReviewStatus.DRAFT, ReviewStatus.IN_REVIEW):
                raise KnowledgeGovernanceError(
                    "new items must start as draft/in_review candidates"
                )
            stored = item.model_copy(deep=True)
            self._items[stored.source_id] = stored
            return stored

    def mark_in_review(self, source_id: str) -> KnowledgeItem:
        with self._lock:
            item = self._require(source_id)
            updated = item.model_copy(
                deep=True, update={"review_status": ReviewStatus.IN_REVIEW}
            )
            self._items[source_id] = updated.model_copy(deep=True)
            return updated

    def list_candidates(self, tenant_id: str | None = None) -> list[KnowledgeItem]:
        with self._lock:
            items = [
                it
                for it in self._items.values()
                if it.review_status is not ReviewStatus.APPROVED
            ]
            if tenant_id is not None:
                items = [it for it in items if it.tenant_id == tenant_id]
            return [it.model_copy(deep=True) for it in items]

    # -- approval / publication -------------------------------------------------

    def approve(
        self,
        source_id: str,
        reviewer: str,
        *,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> KnowledgeItem:
        """Clinical approval. Produces the next immutable knowledge_version;
        any previous approved version is superseded (kept in history)."""
        if not reviewer or not reviewer.strip():
            raise KnowledgeGovernanceError("reviewer is required")
        if valid_from is not None and valid_to is not None and valid_from >= valid_to:
            raise KnowledgeGovernanceError("valid_from must precede valid_to")
        with self._lock:
            item = self._require(source_id)
            prior = self._items[source_id]
            history = self._history.setdefault(source_id, [])
            if prior.review_status is ReviewStatus.APPROVED:
                # a re-approval supersedes the previous approved version, which
                # is moved to history (ordered supersede semantics)
                history.append(prior)
            approved_count = sum(
                1 for h in history if h.review_status is ReviewStatus.APPROVED
            )
            version_no = approved_count + 1
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
            self._items[source_id] = approved.model_copy(deep=True)
            return approved

    # -- production view ---------------------------------------------------------

    def production_items(self, tenant_id: str | None = None) -> list[KnowledgeItem]:
        """APPROVED + in-window items. This is the only view #53 RAG may query."""
        with self._lock:
            items = [
                it for it in self._items.values() if it.is_production_ready(self.now())
            ]
            if tenant_id is not None:
                items = [it for it in items if it.tenant_id == tenant_id]
            return [it.model_copy(deep=True) for it in items]

    def get(self, source_id: str) -> KnowledgeItem:
        with self._lock:
            return self._require(source_id)

    def history(self, source_id: str) -> list[KnowledgeItem]:
        with self._lock:
            history = list(self._history.get(source_id, []))
            current = self._items.get(source_id)
            if current is not None:
                history.append(current)
            return [it.model_copy(deep=True) for it in history]

    def revoke(self, source_id: str, reason: str) -> KnowledgeItem:
        """Remove from the production view immediately; the item stays as an
        auditable revoked record and the source_id can be re-entered."""
        if not reason or not reason.strip():
            raise KnowledgeGovernanceError("revocation reason is required")
        with self._lock:
            item = self._require(source_id)
            revoked = item.model_copy(
                deep=True,
                update={
                    "review_status": ReviewStatus.REVOKED,
                    "knowledge_version": None,
                },
            )
            history = self._history.setdefault(source_id, [])
            history.append(item.model_copy(deep=True))
            self._items.pop(source_id, None)
            history.append(revoked.model_copy(deep=True))
            return revoked

    def _require(self, source_id: str) -> KnowledgeItem:
        item = self._items.get(source_id)
        if item is None:
            raise KnowledgeGovernanceError(f"source {source_id!r} not found")
        return item
