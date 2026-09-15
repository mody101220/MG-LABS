"""Approval API (contract §17). Only OWNER decides approvals."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, require_owner
from app.generated import models

router = APIRouter(tags=["Approvals"])


@router.get("/approvals", operation_id="listApprovals", response_model=models.ApprovalListResponse)
async def list_approvals(request: Request,
                         status: models.ApprovalStatus | None = Query(default="PENDING"),
                         type: models.ApprovalType | None = Query(default=None),
                         page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                         principal: Principal = Depends(require_owner)):
    rows, _total = await request.app.state.services["approvals"].list(
        status.value if status else None, type.value if type else None, page, page_size)
    return build_success(models.ApprovalListResponse, rows)


@router.post("/approvals/{approval_id}/approve", operation_id="approveApproval", response_model=models.ApprovalDecisionResponse)
async def approve_approval(request: Request, approval_id: str, body: models.ApproveRequest,
                           principal: Principal = Depends(require_owner)):
    if not body.approved:
        from app.core.errors import validation_error

        raise validation_error({"field": "approved", "reason": "must be true on this endpoint"})
    decided = await request.app.state.services["approvals"].decide(principal, approval_id, True, None)
    return build_success(models.ApprovalDecisionResponse, {
        "approval_id": decided["id"], "status": "APPROVED",
        "decided_at": decided["decided_at"].isoformat().replace("+00:00", "Z"), "execution_id": None,
    })


@router.post("/approvals/{approval_id}/reject", operation_id="rejectApproval", response_model=models.ApprovalDecisionResponse)
async def reject_approval(request: Request, approval_id: str, body: models.RejectRequest,
                          principal: Principal = Depends(require_owner)):
    decided = await request.app.state.services["approvals"].decide(principal, approval_id, False, body.reason)
    return build_success(models.ApprovalDecisionResponse, {
        "approval_id": decided["id"], "status": "REJECTED",
        "decided_at": decided["decided_at"].isoformat().replace("+00:00", "Z"), "execution_id": None,
    })

# Security registry (contract-drift test reads this at import time).
register("GET", "/approvals", "AUTH")
register("POST", "/approvals/{approval_id}/approve", "AUTH")
register("POST", "/approvals/{approval_id}/reject", "AUTH")
