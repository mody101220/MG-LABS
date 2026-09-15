"""Auth API (contract §3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["Auth"])


@router.post("/auth/login", operation_id="login", response_model=models.LoginResponse, status_code=200)
async def login(request: Request, body: models.LoginRequest):
    services = request.app.state.services
    data = await services["auth"].login(body.email, body.password)
    return build_success(models.LoginResponse, data)


@router.post("/auth/refresh", operation_id="refreshToken", response_model=models.LoginResponse, status_code=200)
async def refresh_token(request: Request, body: models.RefreshRequest):
    services = request.app.state.services
    data = await services["auth"].refresh(body.refresh_token)
    return build_success(models.LoginResponse, data)


@router.post("/auth/logout", operation_id="logout", response_model=models.LogoutResponse, status_code=200)
async def logout(request: Request, principal: Principal = Depends(get_principal)):
    raw = request.headers.get("Authorization", "")
    raw_token = raw.split(" ", 1)[1] if raw.lower().startswith("bearer ") else None
    services = request.app.state.services
    await services["auth"].logout(principal.user_id, raw_token)
    return build_success(models.LogoutResponse, None)

# Security registry (contract-drift test reads this at import time).
register("POST", "/auth/login", "PUBLIC")
register("POST", "/auth/refresh", "PUBLIC")
register("POST", "/auth/logout", "AUTH")
