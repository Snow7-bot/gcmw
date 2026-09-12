"""Identity bounds shared by every contract that carries a tenant/device (#66).

``tenant_id`` and ``device_id`` appear in the session API, in every SSE event and
in the run repository's storage keys. Declaring the bounds ONCE keeps the
credential loader, the response models and the storage layer from drifting
apart — a credential that cannot be represented by the API must be rejected
when the application loads it, not when a request is already being served.
"""

from __future__ import annotations

MAX_TENANT_ID_LENGTH = 64
MAX_DEVICE_ID_LENGTH = 128
MIN_IDENTITY_LENGTH = 1

__all__ = [
    "MAX_DEVICE_ID_LENGTH",
    "MAX_TENANT_ID_LENGTH",
    "MIN_IDENTITY_LENGTH",
]
