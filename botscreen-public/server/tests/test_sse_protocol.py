"""Tests for the dual-layer SSE protocol contract (issue #30)."""

import pytest
from pydantic import ValidationError

from app.contracts.events import (
    EVENT_DATA_ALLOWED_KEYS,
    FORBIDDEN_DATA_KEYS,
    SSE_PROTOCOL_VERSION,
    TERMINAL_EVENTS,
    SSEEvent,
    SSEEventType,
    allowed_layers,
    is_terminal_event,
)


def _event(event: SSEEventType, layer: str = "process", data=None, **overrides):
    fields = {
        "seq": 1,
        "tenant_id": "t1",
        "device_id": "d1",
        "session_id": "s1",
        "run_id": "run-1",
        "layer": layer,
        "event": event,
        "data": data or {},
    }
    fields.update(overrides)
    return fields


class TestProtocolVersion:
    def test_default_version_is_pinned(self):
        evt = SSEEvent(**_event(SSEEventType.PROCESS_STATUS))
        assert evt.protocol_version == SSE_PROTOCOL_VERSION

    def test_unknown_version_rejected(self):
        with pytest.raises(ValidationError):
            SSEEvent(**_event(SSEEventType.PROCESS_STATUS, protocol_version="9.9"))


class TestLayerMap:
    def test_process_events_accept_process_layer(self):
        for event in (
            SSEEventType.RUN_ACCEPTED,
            SSEEventType.PROCESS_STATUS,
            SSEEventType.EVIDENCE_FOUND,
            SSEEventType.REFLECTION_RESULT,
            SSEEventType.RUN_COMPLETED,
            SSEEventType.HEARTBEAT,
        ):
            SSEEvent(**_event(event, layer="process"))

    def test_answer_events_accept_answer_layer(self):
        for event in (SSEEventType.ANSWER_DELTA, SSEEventType.ANSWER_COMPLETED):
            SSEEvent(
                **_event(
                    event,
                    layer="answer",
                    data={"delta": "x"} if event == SSEEventType.ANSWER_DELTA else None,
                )
            )

    def test_answer_event_on_process_layer_rejected(self):
        with pytest.raises(ValidationError, match="not allowed on layer"):
            SSEEvent(**_event(SSEEventType.ANSWER_DELTA, layer="process"))

    def test_process_event_on_answer_layer_rejected(self):
        with pytest.raises(ValidationError, match="not allowed on layer"):
            SSEEvent(**_event(SSEEventType.EVIDENCE_FOUND, layer="answer"))

    def test_map_covers_every_event_type(self):
        for event in SSEEventType:
            assert allowed_layers(event), f"{event.value} missing layer mapping"

    def test_invalid_layer_string_rejected(self):
        with pytest.raises(ValidationError):
            SSEEvent(**_event(SSEEventType.RUN_ACCEPTED, layer="invalid"))


class TestHeartbeat:
    def test_heartbeat_recognisable_and_data_free(self):
        evt = SSEEvent(**_event(SSEEventType.HEARTBEAT))
        assert evt.event is SSEEventType.HEARTBEAT
        assert evt.data == {}
        assert not is_terminal_event(SSEEventType.HEARTBEAT)

    def test_heartbeat_with_data_rejected(self):
        with pytest.raises(ValidationError, match="not allowed"):
            SSEEvent(**_event(SSEEventType.HEARTBEAT, data={"ping": 1}))


class TestTerminality:
    def test_run_completed_is_the_only_run_terminal_event(self):
        assert TERMINAL_EVENTS == frozenset({SSEEventType.RUN_COMPLETED})
        assert is_terminal_event(SSEEventType.RUN_COMPLETED)
        for event in SSEEventType:
            if event is SSEEventType.RUN_COMPLETED:
                continue
            assert not is_terminal_event(event), f"{event.value} must not be terminal"


class TestDataAllowlist:
    def test_allowlisted_keys_pass(self):
        SSEEvent(
            **_event(SSEEventType.EVIDENCE_FOUND, data={"count": 3, "sources": []})
        )
        SSEEvent(
            **_event(
                SSEEventType.ANSWER_DELTA, layer="answer", data={"delta": "近视后"}
            )
        )

    def test_unknown_data_key_rejected(self):
        with pytest.raises(ValidationError, match="not allowed"):
            SSEEvent(**_event(SSEEventType.REFLECTION_RESULT, data={"scores": 1.0}))

    def test_forbidden_internal_keys_rejected_on_any_event(self):
        for key in (
            "chain_of_thought",
            "thinking",
            "prompt",
            "system_prompt",
            "raw",
            "tool_arguments",
        ):
            with pytest.raises(ValidationError, match="forbidden data key"):
                SSEEvent(**_event(SSEEventType.PROCESS_STATUS, data={key: "secret"}))

    def test_forbidden_key_set_is_disjoint_from_allowlists(self):
        for allowed in EVENT_DATA_ALLOWED_KEYS.values():
            assert not (allowed & FORBIDDEN_DATA_KEYS)

    def test_every_event_has_allowlist(self):
        for event in SSEEventType:
            assert event in EVENT_DATA_ALLOWED_KEYS


class TestValueDomains:
    def test_answer_delta_requires_nonempty_delta(self):
        with pytest.raises(ValidationError, match="non-empty string delta"):
            SSEEvent(**_event(SSEEventType.ANSWER_DELTA, layer="answer", data={}))
        with pytest.raises(ValidationError, match="non-empty string delta"):
            SSEEvent(
                **_event(
                    SSEEventType.ANSWER_DELTA, layer="answer", data={"delta": "   "}
                )
            )

    def test_content_origin_value_domain_enforced(self):
        with pytest.raises(ValidationError, match="invalid content_origin"):
            SSEEvent(
                **_event(
                    SSEEventType.ANSWER_COMPLETED,
                    layer="answer",
                    data={"content_origin": "origin:leaked-internal"},
                )
            )
        # optional key: absent content_origin stays valid
        SSEEvent(
            **_event(
                SSEEventType.ANSWER_COMPLETED,
                layer="answer",
                data={"citations": []},
            )
        )


class TestMisc:
    def test_seq_must_be_positive(self):
        with pytest.raises(ValidationError):
            SSEEvent(**_event(SSEEventType.RUN_ACCEPTED, seq=0))

    def test_roundtrip(self):
        evt = SSEEvent(
            **_event(
                SSEEventType.ANSWER_COMPLETED,
                layer="answer",
                data={"citations": [], "actions": [], "content_origin": "ai_generated"},
            )
        )
        assert SSEEvent.model_validate(evt.model_dump()) == evt
