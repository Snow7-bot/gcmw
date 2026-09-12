"""Per-scope request rate limiting (#66 remainder — minimal slice).

Three independent fixed windows, all keyed from the AUTHENTICATED principal:

- ``tenant``  — every request from a tenant;
- ``device``  — every request from one device;
- ``session`` — requests that name one session (explicitly, or through a run).

Counting happens after authentication and before any work, so a throttled
caller cannot create runs, open streams or touch storage. Every ATTEMPT is
charged, including the ones a narrower scope already rejected: a tenant or
device budget limits attempts, so a hammered session cannot hide behind its own
window. Exceeding a window
raises ``E_RATE_LIMIT_EXCEEDED`` (429) carrying ``retry_after_ms`` — the only
error whose envelope carries a wait hint — and emits one structured
:class:`~app.contracts.audit.AuditRecord`.

Deliberately NOT here: distributed/shared counters (a multi-worker deployment
needs the persistent store that #65 gates), credential issuance/rotation, and a
durable audit sink (records go to the ``gcmw.audit`` logger today).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.contracts.audit import AuditRecord
from app.contracts.errors import ErrorCode

from .auth import DevicePrincipal
from .errors import AppError

#: scopes are independent: exhausting one never charges another
SCOPE_TENANT = "tenant"
SCOPE_DEVICE = "device"
SCOPE_SESSION = "session"

#: how many distinct keys are tracked before expired windows are pruned; a cap
#: (not a policy) that keeps a long-running robot's memory bounded
DEFAULT_MAX_KEYS = 10_000

AuditSink = Callable[[AuditRecord], None]


@dataclass(frozen=True)
class RateLimitRule:
    """One fixed window: ``limit`` requests per ``window_s`` seconds."""

    scope: str
    limit: int
    window_s: float

    def __post_init__(self) -> None:
        if self.scope not in {SCOPE_TENANT, SCOPE_DEVICE, SCOPE_SESSION}:
            raise ValueError(f"unknown rate-limit scope {self.scope!r}")
        if self.limit <= 0:
            raise ValueError("rate-limit limit must be positive")
        if not self.window_s > 0:
            raise ValueError("rate-limit window must be positive")


@dataclass
class _Window:
    started_at: float
    count: int = 0


class RateLimiter:
    """Fixed-window counters per ``(scope, key)`` (process-local)."""

    def __init__(
        self,
        rules: tuple[RateLimitRule, ...],
        *,
        clock: Callable[[], float] = time.monotonic,
        audit: AuditSink | None = None,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self._rules = rules
        self._clock = clock
        self._audit = audit
        self._max_keys = max_keys
        self._windows: dict[tuple[str, str], _Window] = {}

    # -- enforcement -----------------------------------------------------------

    def enforce(
        self,
        principal: DevicePrincipal,
        *,
        session_id: str | None = None,
        request_id: str = "",
    ) -> None:
        """Charge every configured scope; raise on the first exhausted window."""
        now = self._clock()
        for rule in self._rules:
            key = self._key_for(rule.scope, principal, session_id)
            if key is None:
                continue
            retry_after_ms = self._charge(rule, key, now)
            if retry_after_ms is not None:
                self._record_audit(principal, rule, session_id, request_id)
                raise AppError(
                    ErrorCode.RATE_LIMIT_EXCEEDED, retry_after_ms=retry_after_ms
                )

    def _key_for(
        self, scope: str, principal: DevicePrincipal, session_id: str | None
    ) -> str | None:
        if scope == SCOPE_TENANT:
            return principal.tenant_id
        if scope == SCOPE_DEVICE:
            return f"{principal.tenant_id}\x1f{principal.device_id}"
        return session_id  # a request that names no session is not session-charged

    def _charge(self, rule: RateLimitRule, key: str, now: float) -> int | None:
        """Return ``retry_after_ms`` when the window is exhausted, else ``None``."""
        entry_key = (rule.scope, key)
        window = self._windows.get(entry_key)
        if window is None or now - window.started_at >= rule.window_s:
            self._prune(now)
            window = _Window(started_at=now)
            self._windows[entry_key] = window
        window.count += 1
        if window.count <= rule.limit:
            return None
        remaining = window.started_at + rule.window_s - now
        return max(1, int(remaining * 1000) + 1)

    def _prune(self, now: float) -> None:
        """Drop expired windows once the table grows past its cap."""
        if len(self._windows) < self._max_keys:
            return
        horizon = max(rule.window_s for rule in self._rules)
        self._windows = {
            key: window
            for key, window in self._windows.items()
            if now - window.started_at < horizon
        }

    # -- inspection / audit ------------------------------------------------------

    def tracked_keys(self) -> int:
        return len(self._windows)

    def _record_audit(
        self,
        principal: DevicePrincipal,
        rule: RateLimitRule,
        session_id: str | None,
        request_id: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit(
            AuditRecord(
                tenant_id=principal.tenant_id,
                actor_type="device",
                actor_id_hash=_digest(principal.device_id),
                session_id_hash=_digest(session_id) if session_id else "",
                request_id=request_id or "unknown",
                action=f"rate_limit.{rule.scope}",
                result="throttled",
                error_code=ErrorCode.RATE_LIMIT_EXCEEDED,
            )
        )


def _digest(value: str) -> str:
    """Stable, non-reversible identifier for audit records."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rules_from_settings(settings: object) -> tuple[RateLimitRule, ...]:
    """Build the configured windows (``0`` disables one scope)."""
    rules: list[RateLimitRule] = []
    for scope, attr in (
        (SCOPE_TENANT, "rate_limit_tenant_per_minute"),
        (SCOPE_DEVICE, "rate_limit_device_per_minute"),
        (SCOPE_SESSION, "rate_limit_session_per_minute"),
    ):
        per_minute = int(getattr(settings, attr, 0) or 0)
        if per_minute > 0:
            rules.append(RateLimitRule(scope=scope, limit=per_minute, window_s=60.0))
    return tuple(rules)


__all__ = [
    "DEFAULT_MAX_KEYS",
    "SCOPE_DEVICE",
    "SCOPE_SESSION",
    "SCOPE_TENANT",
    "RateLimitRule",
    "RateLimiter",
    "rules_from_settings",
]
