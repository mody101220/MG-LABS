"""Email API (contract §§11-13)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.envelope import build_success
from app.core.errors import validation_error
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Email"])


def _s(x):
    """Generated ID fields are pydantic RootModel[str]; unwrap for services/SQL."""
    return x.root if x is not None and hasattr(x, "root") else x


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    return idempotency_key


@router.get("/email/messages", operation_id="listEmailMessages", response_model=models.EmailListResponse)
async def list_email_messages(request: Request,
                              folder: models.EmailFolder | None = Query(default=None),
                              priority: models.EmailPriority | None = Query(default=None),
                              page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                              principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["email"].list_messages(
        folder.value if folder else None, priority.value if priority else None, page, page_size)
    return build_success(models.EmailListResponse, rows)


@router.get("/email/messages/{message_id}", operation_id="getEmailMessage", response_model=models.EmailGetResponse)
async def get_email_message(request: Request, message_id: str, principal: Principal = Depends(get_principal)):
    row = await request.app.state.services["email"].get_message(message_id)
    return build_success(models.EmailGetResponse, row)


@router.post("/email/drafts", operation_id="createEmailDraft", response_model=models.DraftResponse, status_code=201)
async def create_email_draft(request: Request, body: models.CreateDraftRequest,
                             principal: Principal = Depends(require_mutating)):
    draft = await request.app.state.services["email"].create_draft(
        principal, body.reply_to, body.to, body.subject, body.body)
    return build_success(models.DraftResponse, draft)


@router.post("/email/send", operation_id="sendEmail", response_model=models.SendEmailResponse)
async def send_email(request: Request, body: models.SendEmailRequest,
                     principal: Principal = Depends(require_mutating),
                     idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    services = request.app.state.services
    idem = services["idempotency"]
    approval_id = _s(body.approval_id)
    stored = await idem.check("email.send", key, {"draft_id": body.draft_id, "approval_id": approval_id})
    if stored is not None:
        return stored
    data = await services["email"].send(principal, body.draft_id, key, approval_id)
    response = build_success(models.SendEmailResponse, data)
    await idem.record("email.send", key, {"draft_id": body.draft_id, "approval_id": approval_id}, 200, response)
    return response

# Security registry (contract-drift test reads this at import time).
register("GET", "/email/messages", "AUTH")
register("GET", "/email/messages/{message_id}", "AUTH")
register("POST", "/email/drafts", "AUTH")
register("POST", "/email/send", "AUTH")
