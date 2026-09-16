"""Gmail integration API. All Google wire calls remain in GmailService/adapter."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal, require_owner
from app.generated import models

router = APIRouter(tags=["Gmail"])


def _require_idempotency_key(value: str | None) -> str:
    if not value or not 8 <= len(value) <= 128:
        from app.core.errors import validation_error
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    return value


@router.get("/integrations/gmail/status", operation_id="gmailStatus", response_model=models.GmailStatusResponse)
async def gmail_status(request: Request, principal: Principal = Depends(get_principal)):
    return build_success(models.GmailStatusResponse, await request.app.state.services["gmail"].status())


@router.get("/integrations/gmail/oauth/start", operation_id="startGmailOAuth",
            response_model=models.GmailOAuthStartResponse)
async def start_gmail_oauth(request: Request, principal: Principal = Depends(require_owner)):
    return build_success(models.GmailOAuthStartResponse,
                         await request.app.state.services["gmail"].start_oauth(principal))


@router.get("/integrations/gmail/oauth/callback", operation_id="completeGmailOAuth",
            response_model=models.GmailOAuthCallbackResponse)
async def complete_gmail_oauth(request: Request, state: str = Query(...), code: str | None = Query(None),
                               error: str | None = Query(None), error_description: str | None = Query(None)):
    # error_description is deliberately not logged or returned: provider text may
    # contain sensitive OAuth details. The one-time state is the CSRF binding.
    data = await request.app.state.services["gmail"].complete_oauth(state, code, error)
    return build_success(models.GmailOAuthCallbackResponse, data)


@router.get("/integrations/gmail/messages", operation_id="listGmailMessages",
            response_model=models.GmailMessageListResponse)
async def list_gmail_messages(request: Request, page_token: str | None = Query(None),
                              max_results: int = Query(50, ge=1, le=100), q: str | None = Query(None),
                              label_ids: str | None = Query(None),
                              principal: Principal = Depends(get_principal)):
    labels = [v.strip() for v in label_ids.split(",") if v.strip()] if label_ids else None
    data = await request.app.state.services["gmail"].list_messages(page_token, max_results, q, labels)
    return build_success(models.GmailMessageListResponse, data)


@router.get("/integrations/gmail/messages/{message_id}", operation_id="getGmailMessage",
            response_model=models.GmailMessageResponse)
async def get_gmail_message(request: Request, message_id: str, format: str = Query("full"),
                            principal: Principal = Depends(get_principal)):
    return build_success(models.GmailMessageResponse,
                         await request.app.state.services["gmail"].get_message(message_id))


@router.get("/integrations/gmail/threads/{thread_id}", operation_id="getGmailThread",
            response_model=models.GmailThreadResponse)
async def get_gmail_thread(request: Request, thread_id: str, format: str = Query("full"),
                           principal: Principal = Depends(get_principal)):
    return build_success(models.GmailThreadResponse,
                         await request.app.state.services["gmail"].get_thread(thread_id))


@router.post("/integrations/gmail/revoke", operation_id="revokeGmail",
             response_model=models.GmailRevokeResponse)
async def revoke_gmail(request: Request, principal: Principal = Depends(require_owner),
                       idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    services = request.app.state.services
    idem = services["idempotency"]
    stored = await idem.check("gmail.revoke", key, {})
    if stored is not None:
        return stored
    await services["gmail"].revoke(principal)
    response = build_success(models.GmailRevokeResponse, {"status": "DISCONNECTED"})
    await idem.record("gmail.revoke", key, {}, 200, response)
    return response


@router.post("/integrations/gmail/sync", operation_id="syncGmail",
             response_model=models.GmailSyncResponse)
async def sync_gmail(request: Request, body: models.GmailSyncRequest | None = None,
                     principal: Principal = Depends(require_owner),
                     idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    body = body or models.GmailSyncRequest()
    services = request.app.state.services
    idem = services["idempotency"]
    payload = {"page_token": body.page_token, "max_results": body.max_results, "q": body.q}
    stored = await idem.check("gmail.sync", key, payload)
    if stored is not None:
        return stored
    data = await services["gmail"].sync(body.page_token, body.max_results or 50, body.q, key, principal)
    response = build_success(models.GmailSyncResponse, data)
    await idem.record("gmail.sync", key, payload, 200, response)
    return response


register("GET", "/integrations/gmail/status", "AUTH")
register("GET", "/integrations/gmail/oauth/start", "AUTH")
register("GET", "/integrations/gmail/oauth/callback", "PUBLIC")
register("GET", "/integrations/gmail/messages", "AUTH")
register("GET", "/integrations/gmail/messages/{message_id}", "AUTH")
register("GET", "/integrations/gmail/threads/{thread_id}", "AUTH")
register("POST", "/integrations/gmail/revoke", "AUTH")
register("POST", "/integrations/gmail/sync", "AUTH")
