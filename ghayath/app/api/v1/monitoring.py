"""Monitoring API (contract §15)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Monitoring"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.post("/monitoring/check", operation_id="monitoringCheck", response_model=models.MonitoringCheckResponse)
async def monitoring_check(request: Request, body: models.MonitoringCheckRequest,
                           principal: Principal = Depends(require_mutating)):
    checks = [c.value for c in body.checks] if body.checks else None
    data = await request.app.state.services["monitoring"].check(
        principal, _v(body.target_type), body.target_id, checks)
    return build_success(models.MonitoringCheckResponse, data)

# Security registry (contract-drift test reads this at import time).
register("POST", "/monitoring/check", "AUTH")
