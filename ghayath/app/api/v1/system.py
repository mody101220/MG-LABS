"""System API (contract §§23-24): health probe + owner-facing system status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["System"])


@router.get("/health", operation_id="health", response_model=models.HealthResponse)
async def health(request: Request):
    app = request.app
    db_ok = await app.state.db.health()

    redis_state = "down"
    if app.state.settings.redis_url:
        try:
            import redis as redis_lib

            client = redis_lib.Redis.from_url(app.state.settings.redis_url, socket_connect_timeout=1)
            if client.ping():
                redis_state = "healthy"
        except Exception:
            redis_state = "down"

    agent_state = "healthy" if app.state.agent_ready() else "degraded"
    scheduler_state = "healthy" if app.state.scheduler.running else "degraded"
    notifications_state = "healthy" if db_ok else "down"

    services = {"database": "healthy" if db_ok else "down", "redis": redis_state,
                "agent": agent_state, "scheduler": scheduler_state, "notifications": notifications_state}
    if not db_ok:
        overall = "down"
    elif all(v == "healthy" for v in services.values()):
        overall = "healthy"
    else:
        overall = "degraded"
    return build_success(models.HealthResponse, {"status": overall, "services": services})


@router.get("/system/status", operation_id="systemStatus", response_model=models.SystemStatusResponse)
async def system_status(request: Request, principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["system"].status()
    return build_success(models.SystemStatusResponse, data)

# Security registry (contract-drift test reads this at import time).
register("GET", "/health", "PUBLIC")
register("GET", "/system/status", "AUTH")
