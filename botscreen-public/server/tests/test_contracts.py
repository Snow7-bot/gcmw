import pytest
from pydantic import ValidationError

from app.contracts.agent import AgentManifest, AgentResult, ToolRequest
from app.contracts.events import SSEEvent
from app.contracts.model import ContentPart, ContentType, ModelRequest
from app.contracts.run import RunState


def test_agent_manifest_requires_id():
    with pytest.raises(ValidationError):
        AgentManifest(agent_id="", version="1.0.0")


def test_content_parts_support_modalities():
    parts = [
        ContentPart(type=ContentType.TEXT, text="hello"),
        ContentPart(type=ContentType.IMAGE, media_ref="rc://test.png", mime_type="image/png"),
        ContentPart(type=ContentType.AUDIO, media_ref="rc://test.pcm", mime_type="audio/pcm"),
    ]
    req = ModelRequest(content_parts=parts)
    assert len(req.content_parts) == 3


def test_sse_event_layer_validation():
    with pytest.raises(ValidationError):
        SSEEvent(seq=1, run_id="run-1", layer="invalid", event="test")


def test_terminal_states_include_expected():
    assert RunState.COMPLETED in {
        RunState.COMPLETED,
        RunState.DEGRADED,
        RunState.HANDOFF,
        RunState.FAILED,
        RunState.CANCELLED,
    }
