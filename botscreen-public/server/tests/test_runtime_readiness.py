"""Environment fail-closed behaviour and readiness (#65B-2 B2-B review round).

An in-memory run repository plus an in-memory session/idempotency store is a
SINGLE-PROCESS arrangement. staging/production must therefore refuse to start,
and readiness must never answer 200 for backends that cannot serve a
multi-worker deployment — otherwise "production ready" is a lie.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.api.v1.agent_api import RunAdmissionService, ready
from app.config import Settings
from app.main import create_app
from app.runtime import readiness_report
from app.storage.run_repository import MemoryRunRepository

#: a valid persistent configuration (non-loopback URLs, local provider) — the
#: point is that even a *fully configured* production env cannot start while the
#: admission store and run repository are in-memory
PERSISTENT_SETTINGS = {
    "active_provider": "local",
    "redis_url": "redis://redis.internal:6379/0",
    "database_url": "postgresql://gcmw@db.internal:5432/gcmw",
}


def _settings(environment: str) -> Settings:
    if environment in {"staging", "production"}:
        return Settings(environment=environment, **PERSISTENT_SETTINGS)
    return Settings(environment=environment)


def _request(app, path: str = "/api/v1/health/ready") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "state": {},
            "app": app,
        }
    )


class TestReadinessReport:
    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_single_process_environments_are_ready(self, environment):
        report = readiness_report(_settings(environment), MemoryRunRepository())
        assert report.ready is True
        assert report.problems == ()
        assert report.run_repository == "memory"
        assert report.admission_store == "memory"

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_persistent_environments_are_never_ready_on_memory(self, environment):
        report = readiness_report(_settings(environment), MemoryRunRepository())
        assert report.ready is False
        joined = " ".join(report.problems)
        assert "run repository is in-memory" in joined
        assert "admission store is in-memory" in joined
        # real authentication is a readiness requirement too (#66)
        assert "no device credentials configured" in joined
        assert report.device_credentials == "missing"

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_persistent_environments_need_credentials_even_with_storage(
        self, environment
    ):
        """Credentials alone are checked independently of the storage story."""

        class _PersistentRepository:
            """Stand-in for a wired persistent repository (not memory)."""

        report = readiness_report(_settings(environment), _PersistentRepository(), None)
        assert report.ready is False
        assert any(
            "no device credentials configured" in problem for problem in report.problems
        )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_configured_credentials_clear_only_that_problem(self, environment):
        class _PersistentRepository:
            """Stand-in for a wired persistent repository (not memory)."""

        class _Store:
            configured = True

        report = readiness_report(
            _settings(environment), _PersistentRepository(), _Store()
        )
        assert report.device_credentials == "configured"
        assert not any(
            "no device credentials configured" in problem for problem in report.problems
        )

    def test_report_never_exposes_counts_or_secrets(self):
        body = readiness_report(
            _settings("production"), MemoryRunRepository()
        ).as_dict()
        assert set(body) == {"status", "checks", "problems"}
        assert set(body["checks"]) == {
            "environment",
            "run_repository",
            "admission_store",
            "device_credentials",
        }


class TestFailClosedStartup:
    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_startup_refuses_to_serve_on_in_memory_backends(self, environment):
        app = create_app(settings=_settings(environment))
        with pytest.raises(RuntimeError) as excinfo, TestClient(app):
            pass
        message = str(excinfo.value)
        assert "refusing to start" in message
        assert "in-memory" in message
        # the app is left without a service rather than half-initialised
        assert app.state.agent_service is None

    def test_development_still_starts(self):
        app = create_app(settings=_settings("development"))
        with TestClient(app) as client:
            assert client.get("/api/v1/health/ready").status_code == 200
            assert app.state.agent_service is not None


class TestReadyEndpoint:
    def test_ready_is_503_when_no_service_is_running(self):
        app = create_app(settings=_settings("test"))
        response = asyncio.run(ready(_request(app)))
        assert response.status_code == 503
        body = response.body.decode()
        assert '"not_ready"' in body

    def test_ready_is_200_for_a_ready_environment(self):
        settings = _settings("test")
        repository = MemoryRunRepository()
        app = create_app(settings=settings)
        app.state.agent_service = RunAdmissionService(repository=repository)
        app.state.readiness = readiness_report(settings, repository)
        response = asyncio.run(ready(_request(app)))
        assert response.status_code == 200
        assert b'"ready"' in response.body

    def test_ready_is_503_even_if_a_persistent_env_somehow_boots(self):
        """Belt and braces: readiness re-checks the ACTUAL backends."""
        settings = _settings("production")
        app = create_app(settings=settings)
        app.state.agent_service = RunAdmissionService(repository=MemoryRunRepository())
        app.state.readiness = readiness_report(settings, MemoryRunRepository())
        response = asyncio.run(ready(_request(app)))
        assert response.status_code == 503
        assert b"run repository is in-memory" in response.body

    def test_ready_recomputes_when_no_report_was_stored(self):
        settings = _settings("production")
        app = create_app(settings=settings)
        app.state.agent_service = RunAdmissionService(repository=MemoryRunRepository())
        app.state.readiness = None
        response = asyncio.run(ready(_request(app)))
        assert response.status_code == 503
