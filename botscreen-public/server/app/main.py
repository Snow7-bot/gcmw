"""FastAPI application assembly (issue #36).

- central exception handling: every failure leaves the process as an
  :class:`~app.contracts.errors.ErrorEnvelope` with a stable ErrorCode and
  safe message only (no raw exceptions, provider text, paths or stacks);
- request/trace ids are minted per request and echoed in headers and error
  envelopes;
- validation errors map to ``E_VALIDATION_INVALID_INPUT``;
- anything uncaught maps to ``E_INTERNAL_UNKNOWN``.
"""

from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agents.registry import RegistryError
from app.api.v1.agent_api import AppError
from app.api.v1.agent_api import router as agent_router
from app.contracts.errors import ErrorCode, ErrorEnvelope, http_status_for
from app.providers.model_gateway import ModelGatewayError
from app.tools.gateway import ToolGatewayError

X_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _request_ids(request: Request) -> tuple[str, str]:
    """Mint request/trace ids exactly once per request.

    A client-supplied X-Request-ID is honoured only when it matches the
    restricted format; anything else is replaced (never echoed back raw).
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


def _envelope_response(
    request: Request, code: ErrorCode, status_override: int | None = None
) -> JSONResponse:
    request_id, trace_id = _request_ids(request)
    envelope = ErrorEnvelope.build(code=code, request_id=request_id, trace_id=trace_id)
    headers = {"X-Request-ID": request_id, "X-Trace-ID": trace_id}
    return JSONResponse(
        status_code=status_override or http_status_for(code),
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="gcmw agent api", version="0.1.0")

    @app.middleware("http")
    async def ids_middleware(request: Request, call_next):
        _request_ids(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    # -- central error mapping ------------------------------------------------

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return _envelope_response(request, exc.code)

    @app.exception_handler(RegistryError)
    async def registry_error_handler(request: Request, exc: RegistryError):
        return _envelope_response(request, exc.code)

    @app.exception_handler(ModelGatewayError)
    async def gateway_error_handler(request: Request, exc: ModelGatewayError):
        return _envelope_response(request, exc.code)

    @app.exception_handler(ToolGatewayError)
    async def tool_error_handler(request: Request, exc: ToolGatewayError):
        return _envelope_response(request, exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _envelope_response(
            request, ErrorCode.VALIDATION_INVALID_INPUT, status_override=400
        )

    @app.exception_handler(Exception)
    async def uncaught_error_handler(request: Request, exc: Exception):
        # never leak the exception; the audit/trace layer (observability) may
        # correlate via trace id
        return _envelope_response(request, ErrorCode.INTERNAL_UNKNOWN)

    app.include_router(agent_router)
    return app


app = create_app()
