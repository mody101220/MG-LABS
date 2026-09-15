"""Inbound WhatsApp webhook (contract §10 / Phase 11).

Pipeline: signature (HMAC-SHA256 over raw body) → sender allow-list → parse →
dedupe on wamid (DB unique) → classify → store → event. Unverified payloads are
never processed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from app.core import context
from app.core.envelope import error_payload, success_payload
from app.core.errors import integration_offline
from app.core.registry import register
from app.generated import models

router = APIRouter(tags=["Webhooks"])


def _rid() -> str:
    return context.request_id_var.get() or context.new_request_id()


@router.post("/webhooks/whatsapp", operation_id="whatsappWebhook", status_code=202)
async def whatsapp_webhook(request: Request):
    settings = request.app.state.settings
    services = request.app.state.services

    if not settings.whatsapp_webhook_secret:
        err = integration_offline("whatsapp")
        return JSONResponse(error_payload(err.code, err.message, err.details, _rid()), status_code=503)

    raw = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    expected = hmac.new(settings.whatsapp_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return JSONResponse(error_payload(
            models.ErrorCode.AUTHENTICATION_FAILED, "Webhook signature validation failed", {}, _rid()), status_code=401)

    try:
        payload = json.loads(raw)
    except Exception:
        return JSONResponse(error_payload(
            models.ErrorCode.VALIDATION_ERROR, "Malformed JSON body", {}, _rid()), status_code=400)

    # Contract WebhookWhatsAppRequest: event, message_id, sender, text, timestamp — all required.
    problems = []
    if payload.get("event") != "message.received":
        problems.append("event must be 'message.received'")
    if not payload.get("message_id"):
        problems.append("message_id is required")
    sender = payload.get("sender") or ""
    if not re.match(r"^\+[1-9]\d{7,14}$", sender):
        problems.append("sender must be a valid E.164 number")
    text = payload.get("text")
    if not text or len(text) > 65536:
        problems.append("text is required (1..65536 chars)")
    if not payload.get("timestamp"):
        problems.append("timestamp is required")
    if problems:
        return JSONResponse(error_payload(
            models.ErrorCode.VALIDATION_ERROR, "Webhook payload violates the contract", {"problems": problems}, _rid()),
            status_code=400)

    message_id = payload["message_id"]

    allow = settings.allowed_senders
    if allow and sender not in allow:
        return JSONResponse(error_payload(
            models.ErrorCode.AUTHENTICATION_FAILED, "Sender is not in the allow-list", {"reason": "sender_not_allowed"},
            _rid()), status_code=401)

    result = await services["whatsapp"].process_inbound(message_id, sender, text)
    message = result["message"]
    data = {
        "status": "ACCEPTED",
        "message_id": message["id"],
        "conversation_id": None,
    }
    return JSONResponse(success_payload(data, _rid()), status_code=202)

# Security registry (contract-drift test reads this at import time).
register("POST", "/webhooks/whatsapp", "WEBHOOK")
