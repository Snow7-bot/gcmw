"""Shared API test harness (not a test module).

Every test gets its OWN application: the run service lives in the FastAPI
lifespan scope, so each ``TestClient`` context builds a fresh service on a fresh
event loop — no asyncio primitive is ever shared between loops, and no test can
leak session/idempotency state into another.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.config import Settings
from app.contracts.errors import ErrorEnvelope
from app.main import create_app
from app.storage.run_repository import (
    MemoryRunRepository,
    RunRepositoryError,
    RunRepositoryFault,
)

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")
OTHER_DEVICE = DevicePrincipal(tenant_id="t1", device_id="OTHER")
OTHER_TENANT = DevicePrincipal(tenant_id="t2", device_id="d1")


class FakeClock:
    """Injectable clock for session TTL tests."""

    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


@dataclass
class Harness:
    client: TestClient
    app: FastAPI
    clock: FakeClock
    repository: Any

    @property
    def service(self) -> Any:
        return self.app.state.agent_service

    def env(self, response) -> ErrorEnvelope:
        return ErrorEnvelope.model_validate(response.json())


@contextmanager
def running_app(
    repository: Any | None = None,
    environment: str = "test",
    *,
    overrides: bool = True,
) -> Iterator[Harness]:
    repository = repository if repository is not None else MemoryRunRepository()
    app = create_app(
        settings=Settings(environment=environment),
        repository_factory=lambda _settings: repository,
    )
    if overrides:
        app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
    clock = FakeClock()
    with TestClient(app, raise_server_exceptions=False) as client:
        # install the clock once the lifespan has built the service
        if app.state.agent_service is not None:
            app.state.agent_service._clock = clock
        yield Harness(client=client, app=app, clock=clock, repository=repository)
    app.dependency_overrides.clear()


def new_session(harness: Harness, channel: str = "text") -> dict:
    res = harness.client.post("/api/v1/sessions", json={"channel": channel})
    assert res.status_code == 201, res.text
    return res.json()


def new_run(
    harness: Harness,
    session_id: str,
    text: str = "hi",
    key: str = "k",
    headers: dict[str, str] | None = None,
) -> dict:
    res = harness.client.post(
        "/api/v1/agent/runs",
        json={
            "session_id": session_id,
            "input": {"type": "text", "text": text},
            "idempotency_key": key,
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    return res.json()


class ForcedStateRepository(MemoryRunRepository):
    """Memory repository with a test-controlled durable state override.

    Simulates an EXTERNAL writer (another worker, or the future ManagerAgent
    advancing a run) without touching the event loop from the test thread: the
    map is plain data, read inside the loop on the next ``state()`` call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forced: dict[str, Any] = {}

    async def state(self, identity):
        forced = self.forced.get(identity.run_id)
        if forced is not None:
            return forced
        return await super().state(identity)


class VanishingRepository(MemoryRunRepository):
    """Memory repository where selected runs expire (NOT_FOUND) on read.

    Models the repository TTL evicting a run while the admission layer still
    holds its bookkeeping entry.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vanished: set[str] = set()

    async def state(self, identity):
        if identity.run_id in self.vanished:
            raise RunRepositoryError(RunRepositoryFault.NOT_FOUND, "expired")
        return await super().state(identity)
