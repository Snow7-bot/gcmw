"""SSE frame parsing helpers shared by the route tests (not a test module)."""

from __future__ import annotations

import json
from typing import Any


def parse_frames(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into frames.

    A comment frame (``: keep-alive``) yields ``{"comment": ...}``; a protocol
    frame yields ``id``/``event``/``data``. Only the keys present on the wire
    appear in the result, so a test can assert that an error frame carries no
    ``id`` at all.
    """
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith(":"):
            frames.append({"comment": block.lstrip(":").strip()})
            continue
        frame: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        if frame:
            frames.append(frame)
    return frames


def protocol_frames(body: str) -> list[dict[str, Any]]:
    """Only the frames that carry a run event (comments dropped)."""
    return [f for f in parse_frames(body) if "event" in f and "comment" not in f]


def event_names(frames: list[dict[str, Any]]) -> list[str]:
    return [f["event"] for f in frames]


def event_ids(frames: list[dict[str, Any]]) -> list[int]:
    return [f["id"] for f in frames]


def event_payload(frame: dict[str, Any]) -> dict[str, Any]:
    """The protocol event's own ``data`` object (the frame ``data:`` line carries
    the whole SSEEvent, so the event payload is nested one level down)."""
    return frame["data"]["data"]
