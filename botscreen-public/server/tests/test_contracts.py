import pytest
from pydantic import ValidationError

from app.contracts.agent import AgentManifest
from app.contracts.audit import AuditRecord
from app.contracts.events import RunEvent, SSEEvent
from app.contracts.model import ContentPart, ContentType, ModelRequest, ModelResponse
from app.contracts.run import RunState


def test_agent_manifest_requires_id():
    with pytest.raises(ValidationError):
        AgentManifest(agent_id="", version="1.0.0")


def test_unknown_fields_forbidden():
    with pytest.raises(ValidationError):
        AgentManifest(agent_id="agent", version="1.0.0", unexpected=True)


def test_content_parts_support_modalities():
    parts = [
        ContentPart(type=ContentType.TEXT, text="hello"),
        ContentPart(
            type=ContentType.IMAGE, media_ref="rc://test.png", mime_type="image/png"
        ),
        ContentPart(
            type=ContentType.AUDIO, media_ref="rc://test.pcm", mime_type="audio/pcm"
        ),
        ContentPart(
            type=ContentType.VIDEO, media_ref="rc://test.mp4", mime_type="video/mp4"
        ),
    ]
    req = ModelRequest(messages=[], content_parts=parts, trace_id="trace-1")
    assert len(req.content_parts) == 4


def test_model_response_requires_model_version():
    with pytest.raises(ValidationError):
        ModelResponse(provider_id="mock", model_id="mock-model", model_version="")


def test_sse_event_layer_validation():
    with pytest.raises(ValidationError):
        SSEEvent(seq=1, run_id="run-1", layer="invalid", event="test")


def test_run_event_requires_seq():
    with pytest.raises(ValidationError):
        RunEvent(run_id="run-1", state=RunState.ACCEPTED, event_seq=0)


def test_audit_record_requires_ids():
    with pytest.raises(ValidationError):
        AuditRecord(tenant_id="", actor_type="user", actor_id_hash="h", request_id="r")


def test_roundtrip_serialization():
    manifest = AgentManifest(agent_id="agent-1", version="1.0.0")
    data = manifest.model_dump()
    restored = AgentManifest.model_validate(data)
    assert restored == manifest


def test_multimodal_content_requires_media_ref_for_media_types():
    from app.contracts.model import ContentType

    with pytest.raises(ValidationError):
        ContentPart(type=ContentType.IMAGE, text="no media")


def test_timestamps_are_timezone_aware_utc():
    from datetime import timezone

    from app.contracts.events import SSEEvent

    event = SSEEvent(seq=1, run_id="run-1", layer="process", event="accepted")
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset() == timezone.utc.utcoffset(None)


def test_roundtrip_common_contracts():
    from app.contracts.common import (
        DeviceContext,
        RunContext,
        SessionContext,
        TenantContext,
    )

    for obj in [
        TenantContext(tenant_id="t1"),
        DeviceContext(tenant_id="t1", device_id="d1"),
        SessionContext(tenant_id="t1", device_id="d1", session_id="s1"),
        RunContext(
            tenant_id="t1",
            device_id="d1",
            session_id="s1",
            run_id="r1",
            request_id="req1",
            idempotency_key="idem1",
            channel="text",
        ),
    ]:
        restored = type(obj).model_validate(obj.model_dump())
        assert restored == obj


def test_roundtrip_agent_contracts():
    from app.contracts.agent import AgentContext, AgentResult, ToolRequest, ToolResult

    for obj in [
        AgentManifest(agent_id="agent-1", version="1.0.0"),
        AgentContext(
            tenant_id="t1",
            device_id="d1",
            session_id="s1",
            run_id="r1",
            channel="text",
        ),
        ToolRequest(tool_name="knowledge.search", arguments={"q": "x"}),
        ToolResult(tool_name="knowledge.search", ok=True, data={"items": []}),
        AgentResult(agent_id="agent-1", status="completed"),
    ]:
        restored = type(obj).model_validate(obj.model_dump())
        assert restored == obj


def test_roundtrip_model_contracts():
    from app.contracts.model import ModelEvent

    req = ModelRequest(messages=[], trace_id="trace-1")
    resp = ModelResponse(
        provider_id="mock",
        model_id="mock-model",
        model_version="1.0.0",
        content="hello",
    )
    event = ModelEvent(
        type="delta", provider_id="mock", model_id="mock-model", model_version="1.0.0"
    )
    for obj in [req, resp, event]:
        restored = type(obj).model_validate(obj.model_dump())
        assert restored == obj


def test_roundtrip_event_contracts():
    from datetime import timezone

    sse = SSEEvent(seq=1, run_id="run-1", layer="process", event="accepted")
    run_evt = RunEvent(run_id="run-1", state=RunState.ACCEPTED, event_seq=1)
    assert sse.timestamp.tzinfo == timezone.utc
    assert run_evt.timestamp.tzinfo == timezone.utc
    assert SSEEvent.model_validate(sse.model_dump()) == sse
    assert RunEvent.model_validate(run_evt.model_dump()) == run_evt


def test_text_content_requires_text():
    with pytest.raises(ValidationError):
        ContentPart(type=ContentType.TEXT, text="   ")
