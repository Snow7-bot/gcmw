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
        ContentPart(type=ContentType.IMAGE, media_ref="rc://test.png", mime_type="image/png"),
        ContentPart(type=ContentType.AUDIO, media_ref="rc://test.pcm", mime_type="audio/pcm"),
        ContentPart(type=ContentType.VIDEO, media_ref="rc://test.mp4", mime_type="video/mp4"),
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
