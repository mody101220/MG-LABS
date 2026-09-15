"""Notifications API: write endpoint plus the v1.1 persisted archive read."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, require_mutating, require_owner_or_viewer
from app.generated import models

router = APIRouter(tags=["Notifications"])


def _s(x):
    """Generated ID fields are pydantic RootModel[str]; unwrap for services/SQL."""
    return x.root if x is not None and hasattr(x, "root") else x


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.get("/notifications", operation_id="getNotifications", response_model=models.EnvelopeNotificationList)
async def get_notifications(
    request: Request,
    severity: models.NotificationSeverity | None = Query(default=None),
    status: models.Status18 | None = Query(default=None),
    project_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_owner_or_viewer),
):
    del principal
    rows = await request.app.state.services["notifications"].list(
        _v(severity), _v(status), project_id, since, limit)
    return build_success(models.EnvelopeNotificationList, rows)


@router.post("/notifications", operation_id="createNotification", response_model=models.NotificationResponse, status_code=201)
async def create_notification(request: Request, body: models.CreateNotificationRequest,
                              principal: Principal = Depends(require_mutating)):
    data = await request.app.state.services["notifications"].create(
        principal, _v(body.severity), _v(body.channel) if body.channel else None,
        body.title, body.message, _s(body.project_id), body.data)
    return build_success(models.NotificationResponse, data)

# Security registry (contract-drift test reads this at import time).
register("GET", "/notifications", "AUTH")
register("POST", "/notifications", "AUTH")
