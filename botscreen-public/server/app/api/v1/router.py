"""API v1 router placeholder.

Actual routes will be added when the Run/Session API is implemented.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("/health/live")
async def live():
    return {"status": "alive"}


@router.get("/health/ready")
async def ready():
    return {"status": "ready", "checks": {}}
