"""Notifications API (contract §20)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Notifications"])


def _s(x):
    """Generated ID fields are pydantic RootModel[str]; unwrap for services/SQL."""
    return x.root if x is not None and hasattr(x, "root") else x


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.post("/notifications", operation_id="createNotification", response_model=models.NotificationResponse, status_code=201)
async def create_notification(request: Request, body: models.CreateNotificationRequest,
                              principal: Principal = Depends(require_mutating)):
    data = await request.app.state.services["notifications"].create(
        principal, _v(body.severity), _v(body.channel) if body.channel else None,
        body.title, body.message, _s(body.project_id), body.data)
    return build_success(models.NotificationResponse, data)

# Security registry (contract-drift test reads this at import time).
register("POST", "/notifications", "AUTH")
