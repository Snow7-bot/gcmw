"""Device authentication + session/run ACL boundary (issue #36c / #66).

Credential model (minimal, deliberately boring):
- the ONLY accepted scheme is ``Authorization: Bearer <credential>``;
- credentials live in the environment, never in :class:`~app.config.Settings`
  (which stores the env var NAME, matching how provider keys already work);
- the store keeps **SHA-256 digests**, never the credentials themselves, and
  comparison is constant-time (``hmac.compare_digest``);
- an unknown credential is ``E_AUTH_INVALID_CREDENTIALS``; a missing or
  malformed header is ``E_AUTH_MISSING_CREDENTIALS``; a valid credential that
  does not own the addressed session/run is ``E_AUTHZ_FORBIDDEN``.

Default deny is unchanged and unconditional: an empty store authenticates
nobody, and staging/production refuse to start without one (see
:mod:`app.runtime`). Rate limiting is NOT part of this slice (#66 keeps it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import Depends, Request

from app.contracts.errors import ErrorCode

from .errors import AppError

#: minimum credential length accepted at load time (a one-character token is a
#: configuration bug, not a credential)
MIN_CREDENTIAL_LENGTH = 16

BEARER_SCHEME = "bearer"


@dataclass(frozen=True)
class DevicePrincipal:
    """Authenticated caller identity: always derived from a credential."""

    tenant_id: str
    device_id: str


@dataclass(frozen=True)
class DeviceCredential:
    """One registered device credential (the credential itself is never kept)."""

    tenant_id: str
    device_id: str
    token_digest: bytes


def digest_credential(credential: str) -> bytes:
    """SHA-256 digest of a presented credential (never stored in the clear)."""
    return hashlib.sha256(credential.encode("utf-8")).digest()


class CredentialStore:
    """Immutable set of registered device credentials."""

    def __init__(self, credentials: Iterable[DeviceCredential]) -> None:
        self._credentials: tuple[DeviceCredential, ...] = tuple(credentials)

    def __len__(self) -> int:
        return len(self._credentials)

    @property
    def configured(self) -> bool:
        return bool(self._credentials)

    @classmethod
    def from_json(cls, raw: str | None) -> CredentialStore:
        """Parse ``[{"tenant_id":…,"device_id":…,"token":…}, …]``.

        The shape is validated eagerly: a malformed entry, a too-short token or
        the same token registered for two different devices aborts startup
        rather than silently weakening authentication.
        """
        if raw is None or not raw.strip():
            return cls(())
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("device credentials must be valid JSON") from exc
        if not isinstance(entries, list):
            raise TypeError("device credentials must be a JSON list")
        credentials: list[DeviceCredential] = []
        seen: dict[bytes, tuple[str, str]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise TypeError(f"device credential #{index} must be an object")
            try:
                tenant_id = str(entry["tenant_id"]).strip()
                device_id = str(entry["device_id"]).strip()
                token = str(entry["token"])
            except KeyError as exc:
                raise ValueError(
                    f"device credential #{index} needs tenant_id/device_id/token"
                ) from exc
            if not tenant_id or not device_id:
                raise ValueError(f"device credential #{index} has an empty identity")
            if len(token) < MIN_CREDENTIAL_LENGTH:
                raise ValueError(
                    f"device credential #{index} is shorter than "
                    f"{MIN_CREDENTIAL_LENGTH} characters"
                )
            digest = digest_credential(token)
            owner = (tenant_id, device_id)
            if digest in seen and seen[digest] != owner:
                raise ValueError(
                    "the same credential may not be registered for two devices"
                )
            seen[digest] = owner
            credentials.append(
                DeviceCredential(
                    tenant_id=tenant_id, device_id=device_id, token_digest=digest
                )
            )
        return cls(credentials)

    @classmethod
    def from_env(cls, env_name: str) -> CredentialStore:
        return cls.from_json(os.getenv(env_name))

    def resolve(self, presented: str) -> DeviceCredential | None:
        """Find the credential matching ``presented`` (constant-time compare).

        Every entry is compared even after a match, so timing does not reveal
        the position of a credential in the store.
        """
        candidate = digest_credential(presented)
        found: DeviceCredential | None = None
        for credential in self._credentials:
            if hmac.compare_digest(candidate, credential.token_digest):
                found = credential
        return found


def presented_credential(request: Request) -> str | None:
    """Extract the bearer credential, or ``None`` when it is unusable."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return None
    credential = value.strip()
    return credential or None


def get_credential_store(request: Request) -> CredentialStore:
    """The lifespan-scoped credential store (empty store = default deny)."""
    store = getattr(request.app.state, "credentials", None)
    if store is None:  # pragma: no cover - the server always runs the lifespan
        return CredentialStore(())
    return store


def require_owner(
    principal: DevicePrincipal, *, tenant_id: str, device_id: str
) -> None:
    """The ONE session/run ownership decision.

    A principal may only touch resources of its own tenant AND device; every
    entry point (status reads, cancel, event stream) funnels through here, so
    the ACL rule exists in exactly one place.
    """
    if principal.tenant_id != tenant_id or principal.device_id != device_id:
        raise AppError(ErrorCode.AUTHZ_FORBIDDEN)


async def get_device_principal(request: Request) -> DevicePrincipal:
    """Authenticate the caller, or deny (default deny, no anonymous path)."""
    credential = presented_credential(request)
    if credential is None:
        raise AppError(ErrorCode.AUTH_MISSING_CREDENTIALS)
    resolved = get_credential_store(request).resolve(credential)
    if resolved is None:
        raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS)
    return DevicePrincipal(tenant_id=resolved.tenant_id, device_id=resolved.device_id)


PrincipalDep = Depends(get_device_principal)
