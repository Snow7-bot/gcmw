"""Tests for the tenancy + audit + publish-integrity KnowledgeStore (#56A).

Coverage:
- candidate DTO: lifecycle fields cannot be injected; whitespace-only
  identities/reasons are rejected by the shared trimmed-non-empty types;
- production predicate: superseded / missing approval metadata records never
  enter the production view;
- strict approval: naive or non-UTC windows fail BEFORE any state change, and
  records are revalidated end-to-end (no model_copy bypass);
- single-transition publish: only IN_REVIEW -> APPROVED succeeds; repeated and
  concurrent approvals conflict instead of minting v2..v10; republication goes
  revoke -> new candidate -> in_review -> approve;
- atomic commit: a failing audit (e.g. over-long actor) leaves items, history
  and audit structures untouched for candidate add and in_review;
- revocation: reason, authenticated actor, time and version are traceable in a
  frozen audit trail; the revoked record keeps its version;
- tenancy: same source_id across tenants stays isolated for CRUD, version
  history, production view AND audit trails;
- trusted context: no API accepts raw tenant_id/session_id strings.
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
    KnowledgeAuditEvent,
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
    store: KnowledgeStore,
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


def _revocation(
    store: KnowledgeStore,
    actor: str = "dr-li",
    reason: str = "临床复核发现过期",
    evidence_ref: str = "",
) -> RevocationDecision:
    return RevocationDecision(actor=actor, reason=reason, evidence_ref=evidence_ref)


def _published(store, t1, source_id: str = "faq-1", actor: str = "owner-1"):
    """candidate -> in_review -> approved (the only legal publish path)."""
    store.add_candidate(t1, _candidate(source_id), actor=actor)
    store.mark_in_review(t1, source_id, actor=actor)
    return store.approve(t1, source_id, _approval(store))


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
        item = store.add_candidate(t1, _candidate(), actor="owner-1")
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
        store.add_candidate(t1, _candidate(), actor="owner-1")
        forged = KnowledgeItem.model_validate(
            {
                **store.get(t1, "faq-1").model_dump(exclude={"content_hash"}),
                "superseded_by": "x-v100",
                "review_status": "approved",
                "knowledge_version": "faq-1-v1",
                "reviewed_by": "dr-li",
                "reviewed_at": store.now(),
                "valid_from": store.now(),
            }
        )
        assert forged.is_production_ready() is False  # superseded => excluded
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        approved = store.approve(t1, "faq-1", _approval(store))
        assert approved.superseded_by is None
        assert [i.source_id for i in store.production_items(t1)] == ["faq-1"]

    def test_duplicate_source_rejected_within_tenant(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, _candidate(), actor="owner-1")

    def test_list_candidates_is_tenant_scoped(self, store, t1, t2):
        store.add_candidate(t1, _candidate("faq-1"), actor="owner-1")
        store.add_candidate(t2, _candidate("faq-2"), actor="owner-2")
        assert [i.source_id for i in store.list_candidates(t1)] == ["faq-1"]
        assert [i.source_id for i in store.list_candidates(t2)] == ["faq-2"]


class TestWhitespaceIdentities:
    @pytest.mark.parametrize("reviewer", ["   ", "\t", "\n", "  dr-li  "])
    def test_approval_reviewer_must_be_trimmed_non_empty(self, store, reviewer):
        now = store.now()
        if reviewer.strip():
            decision = ApprovalDecision(reviewer=reviewer, valid_from=now)
            assert decision.reviewer == "dr-li"  # trimmed by the contract
        else:
            with pytest.raises(ValidationError):
                ApprovalDecision(reviewer=reviewer, valid_from=now)

    @pytest.mark.parametrize(
        ("actor", "reason"),
        [("   ", "过期"), ("dr-li", "   "), ("\t", "\n")],
    )
    def test_revocation_actor_and_reason_must_be_trimmed_non_empty(
        self, store, actor, reason
    ):
        with pytest.raises(ValidationError):
            RevocationDecision(actor=actor, reason=reason)

    def test_whitespace_reviewer_cannot_reach_production(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        now = store.now()
        with pytest.raises(ValidationError):
            ApprovalDecision(reviewer="   ", valid_from=now)
        assert store.production_items(t1) == []
        assert store.get(t1, "faq-1").review_status is ReviewStatus.IN_REVIEW

    def test_item_reviewed_by_rejects_whitespace(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        with pytest.raises(ValidationError):
            KnowledgeItem.model_validate(
                {
                    **store.get(t1, "faq-1").model_dump(exclude={"content_hash"}),
                    "review_status": "approved",
                    "reviewed_by": "   ",
                    "reviewed_at": store.now(),
                    "knowledge_version": "faq-1-v1",
                }
            )

    def test_audit_actor_rejects_whitespace(self, store, t1):
        with pytest.raises(ValidationError):
            store.add_candidate(t1, _candidate(), actor="   ")


class TestAtomicCommit:
    def test_candidate_add_failure_leaves_all_structures_untouched(self, store, t1):
        with pytest.raises(ValidationError):
            store.add_candidate(t1, _candidate(), actor="a" * 200)  # audit invalid
        assert store._items == {}
        assert store._history == {}
        assert store._audit == {}
        with pytest.raises(KnowledgeGovernanceError):
            store.audit_trail(t1, "faq-1")

    def test_mark_in_review_failure_leaves_all_structures_untouched(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        before = store.get(t1, "faq-1").review_status
        with pytest.raises(ValidationError):
            store.mark_in_review(t1, "faq-1", actor="a" * 200)
        assert store.get(t1, "faq-1").review_status is before
        assert len(store.audit_trail(t1, "faq-1")) == 1  # only candidate.added
        assert store._history == {}


class TestProductionPredicate:
    def test_superseded_record_not_production_ready(self, store):
        now = store.now()
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
        "missing", ["knowledge_version", "reviewed_by", "reviewed_at"]
    )
    def test_missing_approval_metadata_not_production_ready(self, store, missing):
        now = store.now()
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
        assert KnowledgeItem.model_validate(payload).is_production_ready(now) is False

    def test_expired_window_leaves_production(self, store, t1, clock):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        store.approve(t1, "faq-1", _approval(store, minutes_valid=30))
        assert store.production_items(t1)
        clock.advance(hours=1)
        assert store.production_items(t1) == []


class TestSingleTransitionPublish:
    def test_draft_cannot_be_approved_directly(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        with pytest.raises(KnowledgeGovernanceError, match="only IN_REVIEW"):
            store.approve(t1, "faq-1", _approval(store))
        assert store.production_items(t1) == []

    def test_second_approval_conflicts_no_extra_versions(self, store, t1):
        _published(store, t1)
        with pytest.raises(KnowledgeGovernanceError, match="only IN_REVIEW"):
            store.approve(t1, "faq-1", _approval(store, reviewer="dr-wang"))
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v1"
        assert len(store.audit_trail(t1, "faq-1")) == 3  # added, in_review, approved

    def test_concurrent_approvals_yield_exactly_one_published_version(self, store, t1):
        store.add_candidate(t1, _candidate(content="并发批准基座"), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")

        def fire(i):
            try:
                return ("ok", store.approve(t1, "faq-1", _approval(store, f"rev-{i}")))
            except KnowledgeGovernanceError:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, range(10)))
        outcomes = [status for status, _ in results]
        assert outcomes.count("ok") == 1
        assert outcomes.count("conflict") == 9
        assert store.get(t1, "faq-1").knowledge_version == "faq-1-v1"
        assert [i.knowledge_version for i in store.production_items(t1)] == ["faq-1-v1"]
        # candidate + in_review + exactly one approval event
        assert len(store.audit_trail(t1, "faq-1")) == 3

    def test_republication_requires_revoke_new_candidate_cycle(self, store, t1):
        _published(store, t1)
        store.revoke(t1, "faq-1", _revocation(store, reason="内容过期"))
        store.add_candidate(t1, _candidate(content="修订后的内容"), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        approved = store.approve(t1, "faq-1", _approval(store, reviewer="dr-wang"))
        assert approved.knowledge_version == "faq-1-v2"
        assert [i.knowledge_version for i in store.production_items(t1)] == ["faq-1-v2"]
        actions = [e.action for e in store.audit_trail(t1, "faq-1")]
        assert actions == [
            AuditAction.CANDIDATE_ADDED,
            AuditAction.IN_REVIEW,
            AuditAction.APPROVED,
            AuditAction.REVOKED,
            AuditAction.CANDIDATE_ADDED,
            AuditAction.IN_REVIEW,
            AuditAction.APPROVED,
        ]

    def test_repeated_mark_in_review_conflicts_without_duplicate_audit(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        trail_before = store.audit_trail(t1, "faq-1")
        with pytest.raises(KnowledgeGovernanceError, match="already IN_REVIEW"):
            store.mark_in_review(t1, "faq-1", actor="owner-1")
        assert store.audit_trail(t1, "faq-1") == trail_before
        assert len(trail_before) == 2  # candidate.added + one in_review

    def test_mark_in_review_on_approved_requires_revoke_first(self, store, t1):
        _published(store, t1)
        with pytest.raises(KnowledgeGovernanceError, match="revoke before re-review"):
            store.mark_in_review(t1, "faq-1", actor="owner-1")
        assert store.production_items(t1)


class TestStrictApprovalContract:
    def test_naive_validity_window_rejected_by_contract(self):
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(
                {
                    "reviewer": "dr-li",
                    "valid_from": datetime.fromisoformat("2026-01-01T00:00:00"),
                }
            )

    def test_non_utc_validity_window_rejected(self):
        plus8 = timezone(timedelta(hours=8))
        with pytest.raises(ValidationError):
            ApprovalDecision(reviewer="dr-li", valid_from=datetime.now(plus8))

    def test_ill_ordered_window_rejected(self, store):
        now = store.now()
        with pytest.raises(ValidationError):
            ApprovalDecision(
                reviewer="dr-li",
                valid_from=now,
                valid_to=now - timedelta(minutes=1),
            )

    def test_naive_window_never_mutates_store(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(
                {
                    "reviewer": "dr-li",
                    "valid_from": datetime.fromisoformat("2026-01-01T00:00:00"),
                }
            )
        assert store.production_items(t1) == []
        assert store._history == {}

    def test_decisions_are_frozen(self, store):
        decision = _approval(store)
        with pytest.raises(ValidationError):
            decision.reviewer = "attacker"
        revocation = _revocation(store)
        with pytest.raises(ValidationError):
            revocation.reason = "changed"


class TestTrustedOperationTime:
    """Audit time must come from the server clock — never from the caller."""

    @pytest.mark.parametrize(
        "extra",
        [
            {"at": "2000-01-01T00:00:00+00:00"},
            {"reviewed_at": "2000-01-01T00:00:00+00:00"},
            {"operation_at": "2000-01-01T00:00:00+00:00"},
        ],
    )
    def test_approval_decision_cannot_carry_a_timestamp(self, extra):
        payload = {
            "reviewer": "dr-li",
            "valid_from": datetime.now(timezone.utc),
            **extra,
        }
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(payload)

    def test_revocation_decision_cannot_carry_a_timestamp(self):
        with pytest.raises(ValidationError):
            RevocationDecision.model_validate(
                {
                    "actor": "dr-li",
                    "reason": "x",
                    "at": "1999-01-01T00:00:00+00:00",
                }
            )

    def test_cannot_backdate_or_postdate_approval(self, store, t1, clock):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        # the store clock is the single authority: move it, not the decision
        clock.advance(days=2)
        approved = store.approve(t1, "faq-1", _approval(store))
        event = store.audit_trail(t1, "faq-1")[-1]
        assert approved.reviewed_at == clock.value == event.at
        assert approved.reviewed_at.year != 2000  # no caller-supplied backdate

    def test_cannot_backdate_revocation(self, store, t1, clock):
        _published(store, t1)
        clock.advance(days=3)
        store.revoke(t1, "faq-1", _revocation(store))
        event = store.audit_trail(t1, "faq-1")[-1]
        assert event.at == clock.value
        assert event.at.year != 1999

    def test_approval_record_and_audit_share_the_server_instant(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        approved = store.approve(t1, "faq-1", _approval(store, evidence_ref="EV-7"))
        event = store.audit_trail(t1, "faq-1")[-1]
        assert approved.reviewed_at == event.at
        assert event.actor == "dr-li" and event.evidence_ref == "EV-7"

    def test_candidate_created_at_equals_its_audit_time(self, store, t1):
        item = store.add_candidate(t1, _candidate(), actor="owner-1")
        event = store.audit_trail(t1, "faq-1")[0]
        assert item.created_at == event.at

    @pytest.mark.parametrize(
        "bad_clock",
        [
            lambda: datetime.now(timezone.utc).replace(tzinfo=None),  # naive
            lambda: datetime.now(timezone(timedelta(hours=8))),  # non-UTC
        ],
    )
    def test_invalid_server_clock_fails_before_any_write(self, t1, bad_clock):
        store = KnowledgeStore(clock=bad_clock)
        with pytest.raises(KnowledgeGovernanceError):
            store.add_candidate(t1, _candidate(), actor="owner-1")
        assert store._items == {} and store._history == {} and store._audit == {}

    def test_production_query_uses_one_trusted_clock_snapshot(self, clock, t1):
        store = KnowledgeStore(clock=clock)
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        store.approve(t1, "faq-1", _approval(store))
        store.add_candidate(t1, _candidate("faq-2"), actor="owner-1")
        reads = {"count": 0}
        real_clock = store._clock

        def counting_clock():
            reads["count"] += 1
            return real_clock()

        store._clock = counting_clock
        assert [i.source_id for i in store.production_items(t1)] == ["faq-1"]
        assert reads["count"] == 1  # one snapshot for the whole view

    @pytest.mark.parametrize(
        "bad_clock",
        [
            lambda: datetime.now(timezone.utc).replace(tzinfo=None),  # naive
            lambda: datetime.now(timezone(timedelta(hours=8))),  # non-UTC
        ],
    )
    def test_production_query_rejects_invalid_clock_structurally(self, bad_clock, t1):
        store = KnowledgeStore(clock=bad_clock)
        with pytest.raises(KnowledgeGovernanceError):
            store.production_items(t1)  # structured, never a raw TypeError

    def test_invalid_clock_blocks_revoke_without_writes(self, clock, t1):
        store = KnowledgeStore(clock=clock)
        _published(store, t1)
        before = (
            store.get(t1, "faq-1").model_dump(),
            [i.model_dump() for i in store.history(t1, "faq-1")],
            len(store.audit_trail(t1, "faq-1")),
        )
        decision = _revocation(store)
        store._clock = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke(t1, "faq-1", decision)
        assert store.get(t1, "faq-1").review_status is ReviewStatus.APPROVED
        assert store._history == {}
        after = (
            store.get(t1, "faq-1").model_dump(),
            [i.model_dump() for i in store.history(t1, "faq-1")],
            len(store.audit_trail(t1, "faq-1")),
        )
        assert before == after
        assert store._items[t1.tenant_id, "faq-1"].review_status is (
            ReviewStatus.APPROVED
        )  # still published, nothing revoked

    def test_invalid_clock_blocks_approve_and_revoke_without_writes(self, clock, t1):
        store = KnowledgeStore(clock=clock)
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        before = (
            store.get(t1, "faq-1").model_dump(),
            len(store.audit_trail(t1, "faq-1")),
            store._history,
        )
        decision = _approval(store)  # built while the clock is still valid
        store._clock = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t1, "faq-1", decision)
        assert store.get(t1, "faq-1").review_status is ReviewStatus.IN_REVIEW
        assert store._history == {}
        after = (
            store.get(t1, "faq-1").model_dump(),
            len(store.audit_trail(t1, "faq-1")),
            store._history,
        )
        assert before == after


class TestRevocationAudit:
    def test_revoke_is_traceable_and_keeps_version(self, store, t1):
        _published(store, t1, actor="owner-1")
        revoked = store.revoke(t1, "faq-1", _revocation(store, evidence_ref="EV-9"))
        assert revoked.review_status is ReviewStatus.REVOKED
        assert revoked.knowledge_version == "faq-1-v1"  # version preserved
        assert store.production_items(t1) == []
        event = store.audit_trail(t1, "faq-1")[-1]
        assert event.action is AuditAction.REVOKED
        assert event.actor == "dr-li"
        assert event.reason == "临床复核发现过期"
        assert event.evidence_ref == "EV-9"
        assert event.knowledge_version == "faq-1-v1"
        assert (event.tenant_id, event.source_id) == ("t1", "faq-1")
        assert event.at.tzinfo is not None

    def test_audit_events_are_frozen(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        event = store.audit_trail(t1, "faq-1")[0]
        with pytest.raises(ValidationError):
            event.actor = "attacker"
        assert isinstance(event, KnowledgeAuditEvent)

    def test_audit_trail_is_append_only_snapshot(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        trail = store.audit_trail(t1, "faq-1")
        trail.clear()
        assert len(store.audit_trail(t1, "faq-1")) == 1

    def test_non_revocation_events_carry_no_reason(self, store, t1):
        _published(store, t1)
        assert all(e.reason is None for e in store.audit_trail(t1, "faq-1"))


class TestTenantIsolationSameSourceId:
    def test_same_source_id_coexists_independently(self, store, t1, t2):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.add_candidate(t2, _candidate(), actor="owner-2")
        assert store.get(t1, "faq-1").tenant_id == "t1"
        assert store.get(t2, "faq-1").tenant_id == "t2"

    def test_publishing_does_not_bleed_across_tenants(self, store, t1, t2):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.add_candidate(t2, _candidate(), actor="owner-2")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        store.approve(t1, "faq-1", _approval(store))
        assert store.production_items(t1)
        assert store.production_items(t2) == []  # t2 still a draft
        assert store.get(t2, "faq-1").review_status is ReviewStatus.DRAFT

    def test_cross_tenant_reads_are_absent(self, store, t1, t2):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.get(t2, "faq-1")
        with pytest.raises(KnowledgeGovernanceError):
            store.mark_in_review(t2, "faq-1", actor="owner-2")
        with pytest.raises(KnowledgeGovernanceError):
            store.approve(t2, "faq-1", _approval(store))
        with pytest.raises(KnowledgeGovernanceError):
            store.revoke(t2, "faq-1", _revocation(store))
        with pytest.raises(KnowledgeGovernanceError):
            store.audit_trail(t2, "faq-1")

    def test_audit_trails_are_tenant_isolated(self, store, t1, t2):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        store.mark_in_review(t1, "faq-1", actor="owner-1")
        store.approve(t1, "faq-1", _approval(store))
        store.add_candidate(t2, _candidate(), actor="owner-2")
        store.revoke(t2, "faq-1", _revocation(store, actor="dr-wang", reason="t2 过期"))
        t1_trail = store.audit_trail(t1, "faq-1")
        t2_trail = store.audit_trail(t2, "faq-1")
        assert [e.actor for e in t1_trail] == ["owner-1", "owner-1", "dr-li"]
        assert [e.actor for e in t2_trail] == ["owner-2", "dr-wang"]
        assert {e.tenant_id for e in t1_trail} == {"t1"}
        assert {e.tenant_id for e in t2_trail} == {"t2"}
        assert store.production_items(t1) and store.production_items(t2) == []


class TestTrustedContextRequired:
    def test_missing_context_rejected(self, store):
        with pytest.raises(KnowledgeGovernanceError):
            store.production_items(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.list_candidates(None)
        with pytest.raises(KnowledgeGovernanceError):
            store.audit_trail(None, "faq-1")

    def test_no_api_takes_raw_tenant_or_session_fields(self, store):
        """Interface convention check: tenancy only arrives through the trusted
        context object. NOTE: keyword-only parameters are a calling convention,
        NOT a security boundary — the authoritative Tool-Schema exclusion of
        tenant/session/reviewer fields is verified in #69."""
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
            "audit_trail",
        ):
            signature = inspect.signature(getattr(store, name))
            assert next(iter(signature.parameters)) == "context"
            assert not (set(signature.parameters) & {"tenant_id", "session_id"})


class TestIntegrity:
    def test_content_hash_is_derived_and_stable(self, store, t1):
        candidate = _candidate(content="固定内容")
        store.add_candidate(t1, candidate, actor="owner-1")
        expected = KnowledgeItem.model_validate(
            {**candidate.model_dump(), "tenant_id": "t1", "review_status": "draft"}
        ).content_hash
        assert store.get(t1, "faq-1").content_hash == expected

    def test_snapshot_copies_do_not_alias_store_state(self, store, t1):
        store.add_candidate(t1, _candidate(), actor="owner-1")
        # KnowledgeItem is not frozen, but stored state must be a deep copy
        store.get(t1, "faq-1")
        fetched = store.get(t1, "faq-1")
        fetched.title = "被改标题"
        assert store.get(t1, "faq-1").title == "标题 faq-1"
