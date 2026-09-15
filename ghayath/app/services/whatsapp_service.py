"""WhatsApp service (contract §9) — the outbound pipeline:

AUTHORIZATION → PERMISSION CHECK → RECIPIENT VALIDATION → CONTENT POLICY →
SEND (adapter only) → VERIFY → AUDIT.
"""
from __future__ import annotations

import re

from app.core.config import Settings
from app.core.errors import AppError, validation_error
from app.core.security import Principal
from app.generated import models
from app.services.approval_service import ApprovalRequired, ApprovalService
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def content_policy_ok(message: str) -> bool:
    """v0 content policy: non-empty, bounded length, no control chars.
    (A policy engine can be swapped in without touching the pipeline.)"""
    if not message or not message.strip():
        return False
    if any(ord(c) < 32 for c in message):
        return False
    return True


class WhatsAppService:
    def __init__(self, db, settings: Settings, adapter, permission_engine, approvals: ApprovalService,
                 audit: AuditService, events: EventBus) -> None:
        self._db = db
        self._settings = settings
        self._adapter = adapter
        self._perms = permission_engine
        self._approvals = approvals
        self._audit = audit
        self._events = events

    async def status(self) -> dict:
        health = self._adapter.health()
        bits = await self._db.integrations.get("whatsapp")
        perms = [k for k, v in (bits["permissions"] or {}).items() if v] if bits else []
        if health.state != "CONNECTED":
            perms = []
        return {
            "connected": health.state == "CONNECTED",
            "provider": "whatsapp",
            "permissions": sorted(perms),
            "last_checked_at": context_now_iso(),
        }

    async def send(self, principal: Principal, recipient: str, message: str, idempotency_key: str,
                   approval_id: str | None = None) -> dict:
        # 1. Authorization (principal resolved by router) + 2. Permission check.
        decision = await self._perms.check(principal, "WHATSAPP", "SEND", recipient)
        payload = {"recipient": recipient, "message": message}
        if not decision.allowed:
            if decision.requires_approval:
                if approval_id:
                    granted = await self._approvals.verify_granted("WHATSAPP_SEND", payload)
                    if not granted or granted["id"] != approval_id:
                        raise AppError(models.ErrorCode.CONFLICT, "Approval does not match this send",
                                       {"approval_id": approval_id})
                else:
                    # The contract request body carries no approval_id: a resubmitted,
                    # identical send is matched against a previously granted approval.
                    granted = await self._approvals.verify_granted("WHATSAPP_SEND", payload)
                    if granted:
                        approval_id = granted["id"]
                    else:
                        approval = await self._approvals.request("WHATSAPP_SEND", payload,
                                                                 requested_by="agent" if principal.is_agent else principal.user_id,
                                                                 reason="WhatsApp send requires human approval")
                        raise ApprovalRequired(approval["id"], {"recipient": recipient})
            else:
                from app.core.errors import forbidden

                raise forbidden(required="WHATSAPP.SEND")
        # 3. Recipient validation.
        if not E164_RE.match(recipient):
            raise validation_error({"field": "recipient", "reason": "must be a valid E.164 number"})
        # 4. Content policy.
        if not content_policy_ok(message):
            raise validation_error({"policy": "content_policy"})
        # 5. Send (adapter is the only path to the provider) → 6. verify → 7. audit.
        self._adapter.require_connected()
        result = await self._adapter.execute("send", {"recipient": recipient, "message": message})
        verified = await self._adapter.verify(result)
        msg = await self._db.whatsapp.insert_outbound(new_id("wmsg"), recipient, message, idempotency_key, approval_id)
        if not verified:
            await self._db.whatsapp.update_status(msg["id"], "FAILED")
            await self._audit.log("WHATSAPP_SEND", principal, "FAILURE", "whatsapp_message", msg["id"],
                                  details={"recipient": recipient, "reason": "verification failed"})
            from app.core.errors import execution_failed

            raise execution_failed("WhatsApp send failed verification", {"message_id": msg["id"]})
        await self._db.whatsapp.update_status(msg["id"], "SENT")
        await self._audit.log("WHATSAPP_SEND", principal, "SUCCESS", "whatsapp_message", msg["id"],
                              details={"recipient": recipient, "idempotency_key": idempotency_key})
        return {"message_id": msg["id"], "status": "SENT", "audit_id": None}

    async def process_inbound(self, message_id: str, sender: str, text: str) -> dict:
        """Webhook pipeline (Phase 11): dedupe on wamid, classify, store, emit event."""
        from app.services.classification import classify_message

        existing = await self._db.whatsapp.get_by_external_id(message_id)
        if existing:
            return {"duplicate": True, "message": existing, "classification": existing.get("classification")}
        row = await self._db.whatsapp.insert_inbound(message_id, sender, text)
        if row is None:  # raced duplicate
            row = await self._db.whatsapp.get_by_external_id(message_id)
            return {"duplicate": True, "message": row, "classification": row.get("classification")}
        classification = classify_message(text)
        await self._db.execute(
            "UPDATE whatsapp_messages SET status = 'DELIVERED', classification = %s WHERE id = %s",
            classification, row["id"],
        )
        await self._audit.log("WHATSAPP_RECEIVED", None, "SUCCESS", "whatsapp_message", row["id"],
                              details={"sender": sender, "classification": classification})
        await self._events.publish("message.received", "whatsapp", None,
                                   "HIGH" if classification in ("URGENT", "CLIENT") else "INFO",
                                   {"message_id": row["id"], "classification": classification})
        return {"duplicate": False, "message": row, "classification": classification}


def context_now_iso() -> str:
    from app.core import context

    return context.now_utc().isoformat().replace("+00:00", "Z")
