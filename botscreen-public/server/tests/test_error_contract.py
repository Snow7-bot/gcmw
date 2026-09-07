"""Tests for the unified error contract (issue #35)."""

import pytest
from pydantic import ValidationError

from app.contracts.agent import ToolResult
from app.contracts.audit import AuditRecord
from app.contracts.errors import (
    ERROR_REGISTRY,
    ErrorCode,
    ErrorEnvelope,
    ErrorSpec,
    http_status_for,
    is_retryable,
    lookup,
    missing_registry_entries,
)
from app.contracts.model import ModelResponse


def _envelope(**overrides):
    fields = {
        "code": ErrorCode.TIMEOUT_PROVIDER,
        "request_id": "req-1",
        "trace_id": "tr-1",
    }
    fields.update(overrides)
    return fields


class TestRegistry:
    def test_registry_is_complete(self):
        assert missing_registry_entries() == []

    def test_every_spec_maps_to_http_and_is_safe(self):
        for code, spec in ERROR_REGISTRY.items():
            assert isinstance(spec, ErrorSpec)
            assert spec.code is code
            assert 400 <= spec.http_status <= 599
            assert spec.message  # every code carries a safe default message
            assert "\n" not in spec.message

    def test_lookup_unknown_code_fails_loudly(self):
        # an ErrorCode member without registry entry is a programming error

        original = dict(ERROR_REGISTRY)
        try:
            ERROR_REGISTRY.clear()
            with pytest.raises(LookupError):
                lookup(ErrorCode.INTERNAL_UNKNOWN)
        finally:
            ERROR_REGISTRY.clear()
            ERROR_REGISTRY.update(original)

    def test_http_and_retryable_mappings(self):
        assert http_status_for(ErrorCode.RATE_LIMIT_EXCEEDED) == 429
        assert http_status_for(ErrorCode.SAFETY_BLOCKED) == 451
        assert http_status_for(ErrorCode.NOT_FOUND_RUN) == 404
        assert is_retryable(ErrorCode.RATE_LIMIT_EXCEEDED) is True
        assert is_retryable(ErrorCode.AUTH_INVALID_CREDENTIALS) is False


class TestEnvelope:
    def test_build_from_registry(self):
        env = ErrorEnvelope.build(
            ErrorCode.TOOL_TIMEOUT, request_id="r1", trace_id="t1", retry_after_ms=500
        )
        assert env.code is ErrorCode.TOOL_TIMEOUT
        assert env.retryable is True
        assert env.retry_after_ms == 500
        assert env.message

    def test_serialization_roundtrip(self):
        env = ErrorEnvelope.build(
            ErrorCode.MODEL_GENERATION_FAILED, request_id="r2", trace_id="t2"
        )
        payload = env.model_dump_json()
        parsed = ErrorEnvelope.model_validate_json(payload)
        assert parsed == env

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            ErrorEnvelope(**{**_envelope(), "details": "provider said: boom"})

    def test_unknown_string_code_rejected(self):
        with pytest.raises(ValueError):
            ErrorCode("E_NOT_IN_THE_ENUM")

    def test_envelope_never_carries_sensitive_keys(self):
        env = ErrorEnvelope.build(
            ErrorCode.PROVIDER_UNREACHABLE, request_id="r3", trace_id="t3"
        )
        payload = env.model_dump()
        for forbidden in (
            "details",
            "stack",
            "traceback",
            "exception",
            "path",
            "provider_raw",
            "inner_message",
        ):
            assert forbidden not in payload
        assert payload["code"] == "E_PROVIDER_UNREACHABLE"
        assert payload["retryable"] is True


class TestContractIntegration:
    def test_model_response_accepts_enum_and_rejects_raw_string(self):
        ok = ModelResponse(
            provider_id="p",
            model_id="m",
            model_version="1",
            error_code=ErrorCode.TIMEOUT_PROVIDER,
        )
        assert ok.error_code is ErrorCode.TIMEOUT_PROVIDER
        with pytest.raises(ValidationError):
            ModelResponse(
                provider_id="p",
                model_id="m",
                model_version="1",
                error_code="E_SOME_ARBITRARY_STRING",
            )

    def test_tool_result_error_code_is_enum(self):
        ok = ToolResult(
            tool_name="knowledge.search",
            ok=False,
            error_code=ErrorCode.TOOL_SCHEMA_REJECTED,
        )
        assert ok.error_code is ErrorCode.TOOL_SCHEMA_REJECTED
        with pytest.raises(ValidationError):
            ToolResult(
                tool_name="knowledge.search", ok=False, error_code="anything-goes"
            )

    def test_audit_record_error_code_is_enum_and_optional(self):
        rec = AuditRecord(
            tenant_id="t",
            actor_type="user",
            actor_id_hash="h",
            request_id="r",
            action="run",
        )
        assert rec.error_code is None
        rec2 = AuditRecord(
            tenant_id="t",
            actor_type="user",
            actor_id_hash="h",
            request_id="r",
            action="run",
            error_code=ErrorCode.SAFETY_BLOCKED,
        )
        assert rec2.error_code is ErrorCode.SAFETY_BLOCKED
        with pytest.raises(ValidationError):
            AuditRecord(
                tenant_id="t",
                actor_type="user",
                actor_id_hash="h",
                request_id="r",
                action="run",
                error_code="E_BAD",
            )
