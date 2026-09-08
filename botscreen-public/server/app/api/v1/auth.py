"""Device authentication boundary (issue #36 / #36c).

Current state (v1 skeleton): NO real credentials exist yet, so this boundary
DENIES by default — routes that depend on a DevicePrincipal are unreachable
until a principal is injected. Tests inject fake principals through FastAPI
dependency_overrides. Full credential verification, rate limiting and the
session ACL are delivered as #36c and MUST precede #40/#55.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from app.contracts.errors import ErrorCode

from .errors import AppError


@dataclass(frozen=True)
class DevicePrincipal:
    tenant_id: str
    device_id: str


async def get_device_principal(request: Request) -> DevicePrincipal:
    """Default-deny dependency.

    No credential scheme is wired yet (that is #36c). Until then the boundary
    refuses every call; tests override this dependency with fake principals.
    """
    raise AppError(ErrorCode.AUTH_MISSING_CREDENTIALS)


PrincipalDep = Depends(get_device_principal)
