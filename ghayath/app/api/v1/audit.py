"""Audit API (contract §21) — read-only view of the append-only log."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["Audit"])


@router.get("/audit", operation_id="listAudit", response_model=models.AuditListResponse)
async def list_audit(request: Request,
                     project_id: str | None = Query(default=None),
                     action: str | None = Query(default=None),
                     actor: str | None = Query(default=None),
                     result: models.AuditResult | None = Query(default=None),
                     from_: datetime | None = Query(default=None, alias="from"),
                     to: datetime | None = Query(default=None),
                     page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["audit"].list(
        project_id, action, actor, result.value if result else None, from_, to, page, page_size)
    return build_success(models.AuditListResponse, rows)

# Security registry (contract-drift test reads this at import time).
register("GET", "/audit", "AUTH")
