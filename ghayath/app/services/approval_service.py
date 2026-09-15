"""Approval Engine (contract §17 / Phase 7).

- Requesting an approval creates a PENDING record (type, exact action payload, 30 min TTL).
- Only OWNER may decide. Deciding a decided approval → CONFLICT (409).
- APPROVAL_REQUIRED errors carry error.details.approval_id; the client resubmits the
  operation referencing the granted approval — never before it is granted.
"""
from __future__ import annotations

from datetime import timedelta

from app.core import context
from app.core.errors import AppError, conflict, not_found
from app.core.security import Principal
from app.generated import models
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id

APPROVAL_TTL = timedelta(minutes=30)


class ApprovalService:
    def __init__(self, db, events: EventBus, audit: AuditService) -> None:
        self._db = db
        self._events = events
        self._audit = audit

    async def request(self, type: str, payload: dict, requested_by: str, reason: str | None = None) -> dict:
        approval = await self._db.approvals.create(
            new_id("appr"), type, payload, reason, requested_by,
            context.now_utc() + APPROVAL_TTL,
        )
        await self._events.publish("approval.required", "agent", payload.get("project_id"), "MEDIUM",
                                   {"approval_id": approval["id"], "type": type})
        await self._audit.log("APPROVAL_REQUESTED", None, "SUCCESS", "approval", approval["id"], details={"type": type})
        return approval

    async def get(self, approval_id: str) -> dict:
        approval = await self._db.approvals.get(approval_id)
        if not approval:
            raise not_found("approval")
        return approval

    async def list(self, status: str | None, type: str | None, page: int, size: int):
        return await self._db.approvals.list(status, type, page, size)

    async def decide(self, principal: Principal, approval_id: str, approved: bool, reason: str | None) -> dict:
        approval = await self.get(approval_id)
        if approval["status"] != "PENDING":
            raise conflict(f"Approval is already {approval['status'].lower()}", {"approval_id": approval_id, "status": approval["status"]})
        decided = await self._db.approvals.decide(
            approval_id, "APPROVED" if approved else "REJECTED", principal.user_id, reason)
        if not decided:
            raise conflict("Approval is no longer pending", {"approval_id": approval_id})
        event = "approval.granted" if approved else "approval.rejected"
        await self._events.publish(event, "user", None, "MEDIUM", {"approval_id": approval_id})
        await self._audit.log("APPROVAL_" + ("GRANT" if approved else "REJECT"), principal, "SUCCESS",
                              "approval", approval_id, details={"reason": reason})
        return decided

    async def verify_granted(self, type: str, payload: dict) -> dict | None:
        """Used by resubmit paths: is there a granted approval for this exact action?"""
        return await self._db.approvals.find_granted(type, payload)


class ApprovalRequired(AppError):
    def __init__(self, approval_id: str, extra: dict | None = None):
        details = {"approval_id": approval_id}
        if extra:
            details.update(extra)
        super().__init__(models.ErrorCode.APPROVAL_REQUIRED, "Human approval is required before proceeding", details)
