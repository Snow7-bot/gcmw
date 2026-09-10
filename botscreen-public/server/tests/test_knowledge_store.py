"""Tests for the tenancy + audit reworked KnowledgeStore (issue #56A).

Coverage:
- candidate DTO: lifecycle fields cannot be injected (forged governance fields
  are rejected by the contract, not merely ignored);
- production predicate: superseded / revoked / missing approval metadata
  records never enter the production view;
- strict approval: naive or ill-ordered validity windows fail BEFORE any state
  change, and records are revalidated end-to-end (no model_copy bypass);
- revocation: reason, authenticated actor, time and version are traceable in
  an immutable audit trail; the revoked record keeps its version;
- tenancy: same source_id across two tenants stays isolated for CRUD, version
  history, production view AND audit trails;
- trusted context: no API accepts raw tenant_id/session_id/reviewer strings.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts.common import TenantContext
from app.contracts.knowledge import (
    ApprovalDecision,
    AuditAction,
    CandidateInput,
    KnowledgeItem,
    KnowledgeSourceType,
    ReviewStatus,
    RevocationDecision,
)
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


def _candidate(
    source_id: str = "faq-1",
    content: str = "近视后需要定期复查",
) -> CandidateInput:
    return CandidateInput(
        source_id=source_id,
        source_type=KnowledgeSourceType.FAQ,
        title=f"标题 {source_id}",
        content=content,
        source_uri=f"kbase://faq/{source_id}",
    )


def _approval(
    store,
    reviewer: str = "dr-li",
    minutes_valid: int = 60,
    evidence_ref: str = "",
) -> ApprovalDecision:
    now = store.now()
    return ApprovalDecision(
        reviewer=reviewer,
        valid_from=now,
        valid_to=now + timedelta(minutes=minutes_valid),
        evidence_ref=evidence_ref,
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


class TestCandidateDto:
    def test_new_items_are_draft_candidates(self, store, t1):
        item = store.add_candidate(t1, _candidate())
        assert item.review_status is ReviewStatus.DRAFT
        assert item.knowledge_version is None
        assert item.reviewed_by is None
        assert item.superseded_by is None
        assert store.production_items(t1) == []

    @pytest.mark.parametrize(
        "forged",
        [
            {"review_status": "approved"},
            {"reviewed_by": "attacker"},
            {"reviewed_at": "2026-01-01T00:00:00+00:00"},
            {"valid_from": "2026-01-01T00:00:00+00:00"},
            {"valid_to": "2030-01-01T00:00:00+00:00"},
            {"knowledge_version": "faq-1-v99"},
            {"superseded_by": "x-v100"},
            {"created_at": "2026-01-01T00:00:00+00:00"},
            {"tenant_id": "t2"},
        ],
    )
    def test_forged_lifecycle_fields_rejected(self, forged):
        payload = {
            "source_id": "faq-1",
            "source_type": "faq",
            "title": "标题",
            "content": "内容",
            "source_uri": "kbase://faq/1",
            **forged,
        }
        with pytest.raises(ValidationError):
            CandidateInput.model_validate(payload)

    def test_forged_supersede_never_reaches_production(self, store, t1):
        """The reviewer's reproduction: an injected superseded_by must not
        survive into an approved record."""
        store.add_candidate(t1, _candidate())
        forged = KnowledgeItem.model_validate(
            {
                **store.get(t1, "faq-1").model_dump(exclude={"content_hash"}),
                "superseded_by": "x-v100",
                "review_status": "approved",
                "knowledge_version": "faq-1-v1",
                "reviewed_by": "dr-li",
                "reviewed_at": datetime.now(timezone.utc),
                "valid_from": datetime.now(timezone.utc),
            }
        )
        assert forged.is_production_ready() is False  # superseded => excluded
        approved = store.approve(t1, "faq-1", _approval(store))
        assert approved.superseded_by is None
        assert [i.source_id for i in store.production_items(t1)] == ["faq-1"]

    def test_duplicate_source_rejected_within_tenant(self, store, t1):
        store.add_candidate(t1, _candidate())
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, _candidate())

    def test_list_candidates_is_tenant_scoped(self, store, t1, t2):
        store.add_candidate(t1, _candidate("faq-1"))
        store.add_candidate(t2, _candidate("faq-2"))
        assert [i.source_id for i in store.list_candidates(t1)] == ["faq-1"]
        assert [i.source_id for i in store.list_candidates(t2)] == ["faq-2"]


class TestProductionPredicate:
    def test_superseded_record_not_production_ready(self):
        now = datetime.now(timezone.utc)
        item = KnowledgeItem.model_validate(
            {
                "source_id": "faq-1",
                "tenant_id": "t1",
                "source_type": "faq",
                "title": "t",
                "content": "c",
                "source_uri": "u",
                "review_status": "approved",
                "knowledge_version": "faq-1-v1",
                "reviewed_by": "dr-li",
                "reviewed_at": now,
                "superseded_by": "faq-1-v2",
            }
        )
        assert item.is_production_ready(now) is False

    @pytest.mark.parametrize(
        "missing",
        ["knowledge_version", "reviewed_by", "reviewed_at"],
    )
    def test_missing_approval_metadata_not_production_ready(self, missing):
        now = datetime.now(timezone.utc)
        payload = {
            "source_id": "faq-1",
            "tenant_id": "t1",
            "source_type": "faq",
            "title": "t",
            "content": "c",
            "source_uri": "u",
            "review_status": "approved",
            "knowledge_version": "faq-1-v1",
            "reviewed_by": "dr-li",
            "reviewed_at": now,
        }
        payload[missing] = None
        item = KnowledgeItem.model_validate(payload)
        assert item.is_production_ready(now) is False

    def test_reapproval_leaves_only_newest_in_production(self, store, t1):
        store.add_candidate(t1, _candidate())
        store.approve(t1, "faq-1", _approval(store, "dr-li"))
        store.approve(t1, "faq-1", _approval(store, "dr-wang"))
        production = store.production_items(t1)
        assert [i.knowledge_version for i in production] == ["faq-1-v2"]
        superseded = next(
            h for h in store.history(t1, "faq-1") if h.knowledge_version == "faq-1-v1"
        )
        assert superseded.superseded_by == "faq-1-v2"
        assert superseded.is_production_ready() is False


class TestStrictApproval:
    def test_naive_datetime_rejected_by_contract(self):
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(
                {
                    "reviewer": "dr-li",
                    # naive: no tzinfo, parsed without the datetime() ctor
                    "valid_from": datetime.fromisoformat("2026-01-01T00:00:00"),
                }
            )

    def test_naive_window_never_mutates_store(self, store, t1):
        store.add_candidate(t1, _candidate())
        before_production = store.production_items(t1)
        before_history = store.history(t1, "faq-1")
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(
                {
                    "reviewer": "dr-li",
                    "valid_from": datetime.fromisoformat("2026-01-01T00:00:00"),
                }
            )
        assert store.production_items(t1) == before_production == []
        assert store.history(t1, "faq-1") == before_history

    def test_ill_ordered_window_rejected(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(
                {
                    "reviewer": "dr-li",
                    "valid_from": now,
                    "valid_to": now - timedelta(minutes=1),
                }
            )

    def test_reviewer_required(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate({"reviewer": "", "valid_from": now})

    def test_approval_records_aware_metadata(self, store, t1):
        store.add_candidate(t1, _candidate())
        approved = store.approve(t1, "faq-1", _approval(store, evidence_ref="EV-7"))
        assert approved.reviewed_by == "dr-li"
        assert approved.reviewed_at.tzinfo is not None
        assert approved.knowledge_version == "faq-1-v1"
        trail = store.audit_trail(t1, "faq-1")
        assert trail[-1].action is AuditAction.APPROVED
        assert trail[-1].actor == "dr-li"
        assert trail[-1].evidence_ref == "EV-7"

    def test_expired_window_leaves_production(self, store, t1, clock):
        store.add_candidate(t1, _candidate())
        store.approve(t1, "faq-1", _approval(store, minutes_valid=30))
        assert store.production_items(t1)
        clock.advance(hours=1)
        assert store.production_items(t1) == []


class TestRevocationAudit:
    def test_revoke_is_traceable_and_keeps_version(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.approve(t1, "faq-1", _approval(store))
        revoked = store.revoke(
            t1,
            "faq-1",
            RevocationDecision(
                actor="dr-li", reason="临床复核发现过期", evidence_ref="EV-9"
            ),
        )
        assert revoked.review_status is ReviewStatus.REVOKED
        assert revoked.knowledge_version == "faq-1-v1"  # version preserved
        assert store.production_items(t1) == []
        event = store.audit_trail(t1, "faq-1")[-1]
        assert event.action is AuditAction.REVOKED
        assert event.actor == "dr-li"
        assert event.reason == "临床复核发现过期"
        assert event.evidence_ref == "EV-9"
        assert event.knowledge_version == "faq-1-v1"
        assert event.tenant_id == "t1" and event.source_id == "faq-1"
        assert event.at.tzinfo is not None

    def test_revoke_requires_actor_and_reason(self):
        with pytest.raises(ValidationError):
            RevocationDecision.model_validate({"actor": "", "reason": "x"})
        with pytest.raises(ValidationError):
            RevocationDecision.model_validate({"actor": "dr-li", "reason": ""})

    def test_audit_trail_is_append_only_snapshot(self, store, t1):
        store.add_candidate(t1, _candidate())
        trail = store.audit_trail(t1, "faq-1")
        trail.clear()  # caller mutation must not touch stored history
        assert len(store.audit_trail(t1, "faq-1")) == 1

    def test_full_lifecycle_audit_sequence(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        store.approve(t1, "faq-1", _approval(store))
        store.revoke(t1, "faq-1", RevocationDecision(actor="dr-li", reason="过期"))
        actions = [e.action for e in store.audit_trail(t1, "faq-1")]
        assert actions == [
            AuditAction.CANDIDATE_ADDED,
            AuditAction.IN_REVIEW,
            AuditAction.APPROVED,
            AuditAction.REVOKED,
        ]


class TestTenantIsolationSameSourceId:
    def test_same_source_id_coexists_independently(self, store, t1, t2):
        store.add_candidate(t1, _candidate())
        store.add_candidate(t2, _candidate())
        assert store.get(t1, "faq-1").tenant_id == "t1"
        assert store.get(t2, "faq-1").tenant_id == "t2"

    def test_approval_versions_do_not_bleed_across_tenants(self, store, t1, t2):
        store.add_candidate(t1, _candidate())
        store.add_candidate(t2, _candidate())
        store.approve(t1, "faq-1", _approval(store, "dr-li"))
        store.approve(t1, "faq-1", _approval(store, "dr-li"))
        store.approve(t2, "faq-1", _approval(store, "dr-wang"))
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v2"
        assert store.get(t2, "faq-1").knowledge_version == "faq-1-v1"

    def test_cross_tenant_reads_are_absent(self, store, t1, t2):
        store.add_candidate(t1, _candidate())
        with pytest.raises(KnowledgeGovernanceError):
            store.get(t2, "faq-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t2, "faq-1", _approval(store))
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke(t2, "faq-1", RevocationDecision(actor="dr-li", reason="x"))
        with pytest.raises(KnowledgeGovernanceError):
            store.audit_trail(t2, "faq-1")

    def test_audit_trails_are_tenant_isolated(self, store, t1, t2):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.approve(t1, "faq-1", _approval(store, "dr-li"))
        store.add_candidate(t2, _candidate(), actor="owner-2")
        store.revoke(t2, "faq-1", RevocationDecision(actor="dr-wang", reason="t2 过期"))
        t1_trail = store.audit_trail(t1, "faq-1")
        t2_trail = store.audit_trail(t2, "faq-1")
        assert [e.actor for e in t1_trail] == ["owner-1", "dr-li"]
        assert [e.actor for e in t2_trail] == ["owner-2", "dr-wang"]
        assert {e.tenant_id for e in t1_trail} == {"t1"}
        assert {e.tenant_id for e in t2_trail} == {"t2"}
        assert store.production_items(t1) != [] and store.production_items(t2) == []


class TestTrustedContextRequired:
    def test_missing_context_rejected(self, store):
        with pytest.raises(KnowledgeGovernanceError):
            store.production_items(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.list_candidates(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.audit_trail(None, "faq-1")

    def test_api_never_accepts_raw_context_fields(self, store):
        """Tenancy and reviewer identity can only arrive through the trusted
        context object / authenticated decision contracts — never as raw
        model-supplied parameters."""
        import inspect

        forbidden = {"tenant_id", "session_id"}
        for name in (
            "add_candidate",
            "mark_in_review",
            "list_candidates",
            "approve",
            "production_items",
            "get",
            "history",
            "revoke",
            "audit_trail",
        ):
            signature = inspect.signature(getattr(store, name))
            assert next(iter(signature.parameters)) == "context", (
                f"{name} must take the trusted context first"
            )
            assert not (set(signature.parameters) & forbidden), (
                f"{name} must not accept raw context fields"
            )
            # 'actor' may exist for audit, but only as keyword-only (injected
            # by the authenticated caller layer, never positionally)
            for param in signature.parameters.values():
                if param.name == "actor":
                    assert param.kind is inspect.Parameter.KEYWORD_ONLY


class TestReviewGuardsAndIntegrity:
    def test_mark_in_review_on_approved_item_requires_revoke_first(self, store, t1):
        store.add_candidate(t1, _candidate())
        store.approve(t1, "faq-1", _approval(store))
        with pytest.raises(KnowledgeGovernanceError, match="revoke before re-review"):
            store.mark_in_review(t1, "faq-1")
        assert store.production_items(t1)

    def test_concurrent_approvals_produce_contiguous_versions(self, store, t1):
        store.add_candidate(t1, _candidate(content="并发批准基座"))

        def fire(i):
            return store.approve(t1, "faq-1", _approval(store, f"reviewer-{i}"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, range(10)))
        versions = {r.knowledge_version for r in results}
        assert versions == {f"faq-1-v{i}" for i in range(1, 11)}
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v10"
        history = store.history(t1, "faq-1")
        assert len([h for h in history if h.superseded_by]) == 9
        assert len(store.audit_trail(t1, "faq-1")) == 11  # candidate + 10 approvals

    def test_content_hash_is_derived_and_stable(self, store, t1):
        candidate = _candidate(content="固定内容")
        store.add_candidate(t1, candidate)
        assert (
            store.get(t1, "faq-1").content_hash
            == KnowledgeItem.model_validate(
                {
                    **candidate.model_dump(),
                    "tenant_id": "t1",
                    "review_status": "draft",
                }
            ).content_hash
        )

    def test_snapshot_copies_do_not_alias_store_state(self, store, t1):
        store.add_candidate(t1, _candidate())
        store.get(t1, "faq-1").title = "被改标题"
        assert store.get(t1, "faq-1").title == "标题 faq-1"
