"""WhatsApp API (contract §9)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from app.core.envelope import build_success
from app.core.errors import validation_error
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["WhatsApp"])


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    return idempotency_key


@router.get("/integrations/whatsapp/status", operation_id="getWhatsappStatus", response_model=models.WhatsAppStatusResponse)
async def get_whatsapp_status(request: Request, principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["whatsapp"].status()
    return build_success(models.WhatsAppStatusResponse, data)


@router.post("/whatsapp/messages/send", operation_id="sendWhatsappMessage", response_model=models.SendWhatsAppResponse)
async def send_whatsapp_message(request: Request, body: models.SendWhatsAppRequest,
                                principal: Principal = Depends(require_mutating),
                                idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    recipient = body.recipient.root if hasattr(body.recipient, "root") else body.recipient
    services = request.app.state.services
    idem = services["idempotency"]
    stored = await idem.check("whatsapp.send", key, {"recipient": recipient, "message": body.message})
    if stored is not None:
        return stored
    data = await services["whatsapp"].send(principal, recipient, body.message, key)
    response = build_success(models.SendWhatsAppResponse, data)
    await idem.record("whatsapp.send", key, {"recipient": recipient, "message": body.message}, 200, response)
    return response

# Security registry (contract-drift test reads this at import time).
register("GET", "/integrations/whatsapp/status", "AUTH")
register("POST", "/whatsapp/messages/send", "AUTH")
