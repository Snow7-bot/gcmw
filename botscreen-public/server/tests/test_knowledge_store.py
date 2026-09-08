"""Tests for knowledge governance candidate/production zoning (issue #56)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.contracts.knowledge import (
    KnowledgeItem,
    KnowledgeSourceType,
    ReviewStatus,
)
from app.knowledge.store import KnowledgeGovernanceError, KnowledgeStore


def _item(
    source_id: str = "faq-1", tenant_id: str = "t1", content: str = "近视后需要定期复查"
) -> KnowledgeItem:
    return KnowledgeItem(
        source_id=source_id,
        tenant_id=tenant_id,
        source_type=KnowledgeSourceType.FAQ,
        title="近视复查",
        content=content,
        source_uri="rc://faq/1.md",
    )


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def store():
    clock = FakeClock()
    s = KnowledgeStore(clock=clock)
    s.clock = clock
    return s


class TestCandidateZone:
    def test_new_items_are_candidates_not_queryable(self, store):
        store.add_candidate(_item())
        assert store.production_items() == []

    def test_duplicate_source_rejected(self, store):
        store.add_candidate(_item("faq-1"))
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(_item("faq-1", content="其他内容"))

    def test_pre_approved_items_rejected(self, store):
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(
                _item().model_copy(update={"review_status": ReviewStatus.APPROVED})
            )

    def test_list_candidates_filters_tenant(self, store):
        store.add_candidate(_item("a", tenant_id="t1"))
        store.add_candidate(_item("b", tenant_id="t2"))
        assert [i.source_id for i in store.list_candidates("t1")] == ["a"]


class TestApprovalAndProduction:
    def test_approve_requires_reviewer(self, store):
        store.add_candidate(_item())
        with pytest.raises(KnowledgeGovernanceError):
            store.approve("faq-1", reviewer="  ")

    def test_approve_makes_item_production_ready(self, store):
        store.add_candidate(_item())
        approved = store.approve(
            "faq-1",
            reviewer="dr-li",
            valid_from=store.clock.value,
            valid_to=store.clock.value + timedelta(days=30),
        )
        assert approved.review_status is ReviewStatus.APPROVED
        assert approved.reviewed_by == "dr-li"
        assert approved.knowledge_version == "faq-1-v1"
        items = store.production_items("t1")
        assert [i.source_id for i in items] == ["faq-1"]

    def test_expired_item_leaves_production(self, store):
        store.add_candidate(_item())
        store.approve(
            "faq-1",
            reviewer="dr-li",
            valid_from=store.clock.value,
            valid_to=store.clock.value + timedelta(days=7),
        )
        store.clock.advance(7 * 24 * 3600 + 1)
        assert store.production_items("t1") == []

    def test_reapproval_versions_and_keeps_history(self, store):
        store.add_candidate(_item(content="v1 内容"))
        first = store.approve("faq-1", reviewer="dr-li")
        assert first.knowledge_version == "faq-1-v1"
        # clinical revision flow: revoke -> re-enter a corrected candidate -> approve
        store.revoke("faq-1", reason="内容修订")
        store.add_candidate(_item(content="v2 修订内容"))
        second = store.approve("faq-1", reviewer="dr-wang")
        assert second.knowledge_version == "faq-1-v2"
        assert second.content == "v2 修订内容"
        history = store.history("faq-1")
        approved_versions = [
            h.knowledge_version
            for h in history
            if h.review_status is ReviewStatus.APPROVED
        ]
        assert approved_versions == ["faq-1-v1", "faq-1-v2"]

    def test_invalid_valid_window_rejected(self, store):
        store.add_candidate(_item())
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(
                "faq-1",
                reviewer="dr-li",
                valid_from=store.clock.value + timedelta(days=5),
                valid_to=store.clock.value,
            )

    def test_tenant_isolation_in_production(self, store):
        store.add_candidate(_item("a", tenant_id="t1"))
        store.add_candidate(_item("b", tenant_id="t2"))
        store.approve("a", reviewer="r1")
        store.approve("b", reviewer="r2")
        assert [i.source_id for i in store.production_items("t1")] == ["a"]
        assert [i.source_id for i in store.production_items("t2")] == ["b"]


class TestReviewGuards:
    def test_mark_in_review_on_approved_item_requires_revoke_first(self, store):
        store.add_candidate(_item())
        store.approve("faq-1", reviewer="dr-li")
        with pytest.raises(KnowledgeGovernanceError, match="revoke before re-review"):
            store.mark_in_review("faq-1")
        assert store.production_items("t1")  # still in production

    def test_superseded_version_is_recorded_on_direct_reapproval(self, store):
        store.add_candidate(_item(content="旧内容"))
        store.approve("faq-1", reviewer="dr-li")
        # direct re-approval supersedes v1 with v2 (same candidate content or
        # revised through an in-review copy in the production workflow)
        second = store.approve("faq-1", reviewer="dr-wang")
        assert second.knowledge_version == "faq-1-v2"
        history = store.history("faq-1")
        old = next(h for h in history if h.knowledge_version == "faq-1-v1")
        assert old.superseded_by == "faq-1-v2"

    def test_concurrent_approvals_produce_contiguous_versions(self, store):
        from concurrent.futures import ThreadPoolExecutor

        store.add_candidate(_item(content="并发批准基座"))

        def fire(i):
            return store.approve("faq-1", reviewer=f"reviewer-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, range(10)))
        versions = {r.knowledge_version for r in results}
        assert versions == {f"faq-1-v{i}" for i in range(1, 11)}
        assert store.get("faq-1").knowledge_version == "faq-1-v10"
        # history() returns the active v10 plus the 9 superseded records
        history = store.history("faq-1")
        superseded = [h for h in history if h.superseded_by]
        assert len(superseded) == 9
        assert (
            len([h for h in history if h.review_status is ReviewStatus.APPROVED]) == 10
        )


class TestRevoke:
    def test_revoke_removes_from_production_keeps_audit(self, store):
        store.add_candidate(_item())
        store.approve("faq-1", reviewer="dr-li")
        assert store.production_items("t1")
        revoked = store.revoke("faq-1", reason="临床复核发现过期")
        assert revoked.review_status is ReviewStatus.REVOKED
        assert store.production_items("t1") == []
        # audit trail: prior approved record + revocation record
        assert len(store.history("faq-1")) >= 2

    def test_revoke_requires_reason(self, store):
        store.add_candidate(_item())
        store.approve("faq-1", reviewer="dr-li")
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke("faq-1", reason="  ")


class TestIntegrity:
    def test_content_hash_is_derived_and_stable(self, store):
        item = store.add_candidate(_item(content="固定内容 abc"))
        assert item.content_hash == item.content_hash
        import hashlib

        assert item.content_hash == hashlib.sha256("固定内容 abc".encode()).hexdigest()

    def test_snapshot_copies_do_not_alias_store_state(self, store):
        store.add_candidate(_item())
        approved = store.approve("faq-1", reviewer="dr-li")
        approved.content = "外部篡改"
        assert store.get("faq-1").content != "外部篡改"
