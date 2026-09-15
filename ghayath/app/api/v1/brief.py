"""Daily Brief API (contract §25)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["Brief"])


@router.get("/brief/daily", operation_id="getDailyBrief", response_model=models.DailyBriefResponse)
async def get_daily_brief(request: Request, date: str | None = Query(default=None),
                          principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["brief"].daily(date)
    return build_success(models.DailyBriefResponse, data)

# Security registry (contract-drift test reads this at import time).
register("GET", "/brief/daily", "AUTH")
