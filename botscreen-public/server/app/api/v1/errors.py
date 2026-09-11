"""API-layer error type shared by routes, auth and app assembly."""

from __future__ import annotations

import re
import uuid

from fastapi import Request

from app.contracts.errors import ErrorCode


class AppError(RuntimeError):
    """Application-level failure with a stable ErrorCode (#35)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)


X_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def request_ids(request: Request) -> tuple[str, str]:
    """Mint request/trace ids exactly once per request (single authority).

    A client-supplied ``X-Request-ID`` is honoured only when it matches the
    restricted format; anything else is replaced and never echoed back raw.
    Error envelopes and mid-stream SSE error frames both use these ids, so a
    failure that happens after the response has started stays correlatable
    with the request that opened it.
    """
    existing = getattr(request.state, "request_id", None)
    if existing is not None:
        return existing, request.state.trace_id
    header = request.headers.get("x-request-id") or ""
    request_id = header if X_REQUEST_ID_RE.match(header) else uuid.uuid4().hex
    trace_id = uuid.uuid4().hex
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    return request_id, trace_id
