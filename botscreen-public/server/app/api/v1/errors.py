"""API-layer error type shared by routes, auth and app assembly."""

from __future__ import annotations

from app.contracts.errors import ErrorCode


class AppError(RuntimeError):
    """Application-level failure with a stable ErrorCode (#35)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)
