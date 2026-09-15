"""Permissions API (contract §§18-19). Read-only in v1: the agent cannot grant
itself permissions (no mutation path exists in the contract)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["Permissions"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.get("/permissions", operation_id="getPermissions", response_model=models.PermissionsResponse)
async def get_permissions(request: Request, principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["permissions"].effective_for(principal)
    return build_success(models.PermissionsResponse, data)


@router.post("/permissions/check", operation_id="checkPermission", response_model=models.PermissionCheckResponse)
async def check_permission(request: Request, body: models.PermissionCheckRequest,
                           principal: Principal = Depends(get_principal)):
    decision = await request.app.state.services["permission_engine"].check(
        principal, _v(body.resource), body.action, body.target)
    return build_success(models.PermissionCheckResponse, {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "requires_approval": decision.requires_approval,
    })

# Security registry (contract-drift test reads this at import time).
register("GET", "/permissions", "AUTH")
register("POST", "/permissions/check", "AUTH")
