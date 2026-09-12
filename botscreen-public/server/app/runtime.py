"""Runtime composition + readiness (#65B-2 slice B2-B, review round).

The application assembly must never *claim* production readiness it does not
have. Both the run repository and the session/idempotency admission store are
in-memory today, which is a single-process development arrangement: N workers
would each keep their own sessions, idempotency keys and run state.

``ReadinessReport`` is the single place that decides this, so app startup and
``/health/ready`` can never disagree. staging/production therefore FAIL CLOSED
until a persistent admission store plus a wired run repository exist — swapping
only the run repository for Redis while sessions/idempotency stay in per-worker
memory would still be unsafe to call available.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.storage.run_repository import MemoryRunRepository

#: environments where the single-process in-memory arrangement is legitimate
DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})

#: where the credential store comes from (see app.api.v1.auth)

#: environments that require persistence on BOTH sides before serving
PERSISTENT_ENVIRONMENTS = frozenset({"staging", "production"})

#: admission store backend. There is exactly one implementation today, and it
#: is in-memory; the Redis AdmissionStore slice will replace this constant with
#: a real backend selection (multi-worker admission is NOT faked here).
ADMISSION_STORE_MEMORY = "memory"

RUN_REPOSITORY_MEMORY = "memory"


def build_run_repository(settings: Settings) -> MemoryRunRepository:
    """Select the run repository implementation for this environment.

    Only the EXISTING implementation can be built today — this is composition,
    not a new storage abstraction. A Redis-backed selection (client lifecycle,
    connection check, config) is a separate slice; until it lands,
    staging/production stop at :func:`readiness_report` instead of quietly
    running on memory.
    """
    return MemoryRunRepository()


@dataclass(frozen=True)
class ReadinessReport:
    """Immutable readiness decision shared by startup and /health/ready."""

    environment: str
    run_repository: str
    admission_store: str
    device_credentials: str
    ready: bool
    problems: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {
                "environment": self.environment,
                "run_repository": self.run_repository,
                "admission_store": self.admission_store,
                "device_credentials": self.device_credentials,
            },
            "problems": list(self.problems),
        }


def readiness_report(
    settings: Settings, run_repository: object, credentials: object | None = None
) -> ReadinessReport:
    """Decide readiness from the environment and the ACTUAL backends in use."""
    backend = (
        RUN_REPOSITORY_MEMORY
        if isinstance(run_repository, MemoryRunRepository)
        else "persistent"
    )
    configured = bool(getattr(credentials, "configured", False))
    credentials_state = "configured" if configured else "missing"
    problems: list[str] = []
    if settings.environment in PERSISTENT_ENVIRONMENTS:
        if backend == RUN_REPOSITORY_MEMORY:
            problems.append(
                "run repository is in-memory (single process only); a wired "
                "persistent RunRepository is required"
            )
        if ADMISSION_STORE_MEMORY == "memory":
            problems.append(
                "session/idempotency admission store is in-memory (unsafe for "
                "multi-worker admission); a persistent AdmissionStore is required"
            )
        if not configured:
            problems.append(
                f"no device credentials configured ({settings.auth_credentials_env} "
                "is empty); real authentication is required"
            )
    return ReadinessReport(
        environment=settings.environment,
        run_repository=backend,
        admission_store=ADMISSION_STORE_MEMORY,
        device_credentials=credentials_state,
        ready=not problems,
        problems=tuple(problems),
    )
