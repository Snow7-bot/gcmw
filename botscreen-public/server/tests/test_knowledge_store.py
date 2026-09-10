"""Tests for the tenancy-reworked KnowledgeStore (issue #56A).

Coverage:
- candidate zone / approval / production view / revoke semantics (unchanged
  governance behaviour, now expressed through the trusted-tenant contract);
- tenancy identity: the key is ``(tenant_id, source_id)`` — two tenants can
  hold the SAME source_id independently across CRUD, version history and the
  production view; every lookup is tenant-scoped;
- trusted context enforcement: operations require a TenantContext, an item
  whose tenant contradicts the context is refused, and no API accepts a raw
  tenant_id or session id from the caller.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.contracts.common import TenantContext
from app.contracts.knowledge import KnowledgeItem, KnowledgeSourceType, ReviewStatus
from app.knowledge.store import KnowledgeGovernanceError, KnowledgeStore


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def _ctx(tenant: str = "t1") -> TenantContext:
    return TenantContext(tenant_id=tenant)


def _item(
    source_id: str = "faq-1",
    tenant_id: str = "t1",
    content: str = "近视后需要定期复查",
) -> KnowledgeItem:
    return KnowledgeItem(
        source_id=source_id,
        tenant_id=tenant_id,
        source_type=KnowledgeSourceType.FAQ,
        title=f"标题 {source_id}",
        content=content,
        source_uri=f"kbase://faq/{source_id}",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> KnowledgeStore:
    return KnowledgeStore(clock=clock)


@pytest.fixture
def t1() -> TenantContext:
    return _ctx("t1")


@pytest.fixture
def t2() -> TenantContext:
    return _ctx("t2")


class TestCandidateZone:
    def test_new_items_are_candidates_not_queryable(self, store, t1):
        store.add_candidate(t1, _item())
        assert store.list_candidates(t1)[0].review_status is ReviewStatus.DRAFT
        assert store.production_items(t1) == []

    def test_duplicate_source_rejected_within_tenant(self, store, t1):
        store.add_candidate(t1, _item())
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, _item())

    def test_pre_approved_items_rejected(self, store, t1):
        item = _item().model_copy(
            deep=True, update={"review_status": ReviewStatus.APPROVED}
        )
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, item)

    def test_item_tenant_must_match_context(self, store, t1):
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, _item(tenant_id="t2"))

    def test_list_candidates_is_tenant_scoped(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="faq-1", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="faq-2", tenant_id="t2"))
        assert [i.source_id for i in store.list_candidates(t1)] == ["faq-1"]
        assert [i.source_id for i in store.list_candidates(t2)] == ["faq-2"]


class TestApprovalAndProduction:
    def test_approve_requires_reviewer(self, store, t1):
        store.add_candidate(t1, _item())
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t1, "faq-1", reviewer="  ")

    def test_approve_makes_item_production_ready(self, store, t1):
        store.add_candidate(t1, _item())
        approved = store.approve(t1, "faq-1", reviewer="dr-li")
        assert approved.review_status is ReviewStatus.APPROVED
        assert approved.knowledge_version == "faq-1-v1"
        production = store.production_items(t1)
        assert [i.source_id for i in production] == ["faq-1"]
        assert production[0].knowledge_version == "faq-1-v1"

    def test_expired_item_leaves_production(self, store, t1, clock):
        store.add_candidate(t1, _item())
        store.approve(
            t1,
            "faq-1",
            reviewer="dr-li",
            valid_to=store.now() + timedelta(hours=1),
        )
        assert store.production_items(t1)
        clock.advance(days=1)
        assert store.production_items(t1) == []

    def test_reapproval_versions_and_keeps_history(self, store, t1):
        store.add_candidate(t1, _item())
        first = store.approve(t1, "faq-1", reviewer="dr-li")
        second = store.approve(t1, "faq-1", reviewer="dr-wang")
        assert (first.knowledge_version, second.knowledge_version) == (
            "faq-1-v1",
            "faq-1-v2",
        )
        old = next(
            h for h in store.history(t1, "faq-1") if h.knowledge_version == "faq-1-v1"
        )
        assert old.superseded_by == "faq-1-v2"

    def test_invalid_valid_window_rejected(self, store, t1):
        store.add_candidate(t1, _item())
        now = store.now()
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t1, "faq-1", reviewer="dr-li", valid_from=now, valid_to=now)

    def test_production_view_only_returns_calling_tenant(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="shared", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="shared", tenant_id="t2"))
        store.approve(t1, "shared", reviewer="dr-li")
        assert [i.source_id for i in store.production_items(t1)] == ["shared"]
        assert store.production_items(t2) == []  # t2's copy is still a candidate


class TestTenantIsolationSameSourceId:
    """The reviewer's core acceptance: two tenants, one source_id, full CRUD +
    history + production isolation."""

    def test_same_source_id_coexists_independently(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="faq-1", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="faq-1", tenant_id="t2"))
        assert store.get(t1, "faq-1").tenant_id == "t1"
        assert store.get(t2, "faq-1").tenant_id == "t2"

    def test_approval_versions_do_not_bleed_across_tenants(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="faq-1", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="faq-1", tenant_id="t2"))
        store.approve(t1, "faq-1", reviewer="dr-li")
        store.approve(t1, "faq-1", reviewer="dr-li")  # t1 -> v2
        store.approve(t2, "faq-1", reviewer="dr-wang")  # t2 must start at v1
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v2"
        assert store.get(t2, "faq-1").knowledge_version == "faq-1-v1"

    def test_history_is_tenant_scoped(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="faq-1", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="faq-1", tenant_id="t2"))
        store.approve(t1, "faq-1", reviewer="dr-li")
        store.approve(t1, "faq-1", reviewer="dr-li")
        # superseded v1 + active v2 — a single re-approval, no cross-tenant rows
        assert len(store.history(t1, "faq-1")) == 2
        assert store.history(t2, "faq-1")[0].review_status is ReviewStatus.DRAFT

    def test_cross_tenant_reads_are_absent(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="faq-1", tenant_id="t1"))
        with pytest.raises(KnowledgeGovernanceError):
            store.get(t2, "faq-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.mark_in_review(t2, "faq-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t2, "faq-1", reviewer="dr-li")
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke(t2, "faq-1", reason="x")

    def test_revoke_is_tenant_scoped(self, store, t1, t2):
        for tenant, ctx in (("t1", t1), ("t2", t2)):
            store.add_candidate(ctx, _item(source_id="faq-1", tenant_id=tenant))
            store.approve(ctx, "faq-1", reviewer="dr-li")
        store.revoke(t1, "faq-1", reason="过期")
        assert store.production_items(t1) == []
        assert [i.source_id for i in store.production_items(t2)] == ["faq-1"]

    def test_production_query_never_returns_other_tenant_rows(self, store, t1, t2):
        store.add_candidate(t1, _item(source_id="a", tenant_id="t1"))
        store.add_candidate(t2, _item(source_id="b", tenant_id="t2"))
        store.approve(t1, "a", reviewer="dr-li")
        store.approve(t2, "b", reviewer="dr-li")
        assert [i.source_id for i in store.production_items(t1)] == ["a"]
        assert [i.source_id for i in store.production_items(t2)] == ["b"]


class TestTrustedContextRequired:
    def test_missing_context_rejected(self, store):
        with pytest.raises(KnowledgeGovernanceError):
            store.production_items(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.list_candidates(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.get(None, "faq-1")

    def test_blank_source_id_rejected(self, store, t1):
        with pytest.raises(KnowledgeGovernanceError):
            store.get(t1, "  ")

    def test_api_never_accepts_raw_tenant_id(self, store):
        """No store method takes a tenant_id/session_id string — tenancy can
        only come from the trusted context object, so a model-supplied
        parameter can never widen or redirect a query."""
        import inspect

        for name in (
            "add_candidate",
            "mark_in_review",
            "list_candidates",
            "approve",
            "production_items",
            "get",
            "history",
            "revoke",
        ):
            params = inspect.signature(getattr(store, name)).parameters
            assert next(iter(params)) == "context", (
                f"{name} must take the trusted context first"
            )
            assert "tenant_id" not in params, f"{name} must not accept tenant_id"
            assert "session_id" not in params, f"{name} must not accept session_id"


class TestReviewGuards:
    def test_mark_in_review_on_approved_item_requires_revoke_first(self, store, t1):
        store.add_candidate(t1, _item())
        store.approve(t1, "faq-1", reviewer="dr-li")
        with pytest.raises(KnowledgeGovernanceError, match="revoke before re-review"):
            store.mark_in_review(t1, "faq-1")
        assert store.production_items(t1)  # still in production

    def test_superseded_version_is_recorded_on_direct_reapproval(self, store, t1):
        store.add_candidate(t1, _item(content="旧内容"))
        store.approve(t1, "faq-1", reviewer="dr-li")
        second = store.approve(t1, "faq-1", reviewer="dr-wang")
        assert second.knowledge_version == "faq-1-v2"
        old = next(
            h for h in store.history(t1, "faq-1") if h.knowledge_version == "faq-1-v1"
        )
        assert old.superseded_by == "faq-1-v2"

    def test_concurrent_approvals_produce_contiguous_versions(self, store, t1):
        store.add_candidate(t1, _item(content="并发批准基座"))

        def fire(i):
            return store.approve(t1, "faq-1", reviewer=f"reviewer-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, range(10)))
        versions = {r.knowledge_version for r in results}
        assert versions == {f"faq-1-v{i}" for i in range(1, 11)}
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v10"
        history = store.history(t1, "faq-1")
        superseded = [h for h in history if h.superseded_by]
        assert len(superseded) == 9
        assert (
            len([h for h in history if h.review_status is ReviewStatus.APPROVED]) == 10
        )


class TestRevoke:
    def test_revoke_removes_from_production_keeps_audit(self, store, t1):
        store.add_candidate(t1, _item())
        store.approve(t1, "faq-1", reviewer="dr-li")
        assert store.production_items(t1)
        revoked = store.revoke(t1, "faq-1", reason="临床复核发现过期")
        assert revoked.review_status is ReviewStatus.REVOKED
        assert store.production_items(t1) == []
        assert len(store.history(t1, "faq-1")) >= 2

    def test_revoke_requires_reason(self, store, t1):
        store.add_candidate(t1, _item())
        store.approve(t1, "faq-1", reviewer="dr-li")
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke(t1, "faq-1", reason="  ")


class TestIntegrity:
    def test_content_hash_is_derived_and_stable(self, store, t1):
        item = _item(content="固定内容")
        store.add_candidate(t1, item)
        again = store.get(t1, "faq-1")
        assert again.content_hash == item.content_hash
        assert len(again.content_hash) == 64

    def test_snapshot_copies_do_not_alias_store_state(self, store, t1):
        store.add_candidate(t1, _item())
        fetched = store.get(t1, "faq-1")
        fetched.title = "被改标题"
        assert store.get(t1, "faq-1").title == "标题 faq-1"
