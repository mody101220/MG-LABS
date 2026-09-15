"""Email service (contract §§11-13): inbox reads (DB store), drafts, and sending.

High-impact sends (or any send by the AGENT without EMAIL.SEND) are blocked with
APPROVAL_REQUIRED (409 + error.details.approval_id). The draft resubmits with the
granted approval_id. The provider adapter is the only path that sends mail.
"""
from __future__ import annotations

from app.core.config import Settings
from app.core.errors import AppError, conflict, not_found
from app.core.security import Principal
from app.generated import models
from app.services.approval_service import ApprovalRequired, ApprovalService
from app.services.audit_service import AuditService
from app.services.ids import new_id

_HIGH_IMPACT_RE = None  # set below (compiled at import)


def _is_high_impact(draft: dict) -> bool:
    import re

    text = f"{draft.get('subject') or ''} {draft.get('body') or ''}".lower()
    patterns = (r"invoice", r"payment", r"contract", r"commit", r"agreement", r"فاتورة", r"دفع", r"عقد", r"التزام")
    return any(re.search(p, text, re.I) for p in patterns)


class EmailService:
    def __init__(self, db, settings: Settings, adapter, permission_engine, approvals: ApprovalService,
                 audit: AuditService) -> None:
        self._db = db
        self._settings = settings
        self._adapter = adapter
        self._perms = permission_engine
        self._approvals = approvals
        self._audit = audit

    async def list_messages(self, folder: str | None, priority: str | None, page: int, size: int):
        return await self._db.email.list_messages(folder or "inbox", priority, page, size)

    async def get_message(self, message_id: str) -> dict:
        row = await self._db.email.get_message(message_id)
        if not row:
            raise not_found("email message")
        return row

    async def create_draft(self, principal: Principal, reply_to: str | None, to: str | None,
                           subject: str | None, body: str) -> dict:
        if not reply_to and not to:
            raise AppError(models.ErrorCode.VALIDATION_ERROR, "Exactly one of reply_to / to is required",
                           {"field": "reply_to|to"})
        reply_row = None
        if reply_to:
            reply_row = await self._db.email.get_message(reply_to)
            if not reply_row:
                raise not_found("email message")
        recipient = to or reply_row["sender"]
        final_subject = subject if (subject and not reply_to) else (f"Re: {reply_row['subject']}" if reply_to else subject)
        draft = await self._db.email.create_draft(new_id("draft"), reply_to, recipient, final_subject or "", body)
        await self._audit.log("EMAIL_DRAFT_CREATED", principal, "SUCCESS", "email_draft", draft["id"],
                              details={"reply_to": reply_to, "to": recipient})
        return _draft_view(draft)

    async def create_for_automation(self, to: str | None, subject: str | None, body: str | None) -> dict:
        """Automation-driven draft creation (scheduler path)."""
        if not to or not body:
            raise AppError(models.ErrorCode.VALIDATION_ERROR, "SEND_EMAIL_DRAFT requires to and body",
                           {"field": "action.params"})
        return await self.create_draft(None, None, to, subject, body)

    async def send(self, principal: Principal, draft_id: str, idempotency_key: str,
                   approval_id: str | None = None) -> dict:
        draft = await self._db.email.get_draft(draft_id)
        if not draft:
            raise not_found("email draft")
        if draft["status"] == "SENT":
            raise conflict("Draft already sent", {"draft_id": draft_id})

        payload = {"draft_id": draft_id, "to": draft["to_addr"], "subject": draft["subject"], "body": draft["body"]}
        decision = await self._perms.check(principal, "EMAIL", "SEND", draft_id)
        needs_approval = _is_high_impact(draft) or (not decision.allowed and decision.requires_approval)
        if needs_approval:
            if approval_id:
                # Exact-payload match: must be identical to what was requested for approval.
                granted = await self._approvals.verify_granted(
                    "EMAIL_SEND", {"draft_id": draft_id, "to": draft["to_addr"], "subject": draft["subject"]})
                if not granted or granted["id"] != approval_id:
                    raise conflict("Approval does not match this draft", {"approval_id": approval_id})
            else:
                approval = await self._approvals.request(
                    "EMAIL_SEND", {"draft_id": draft_id, "to": draft["to_addr"], "subject": draft["subject"]},
                    requested_by="agent" if principal.is_agent else principal.user_id,
                    reason="Email send requires human approval" + (" (high-impact content)" if _is_high_impact(draft) else ""))
                await self._db.email.update_draft(draft_id, "APPROVAL_PENDING", approval_id=approval["id"])
                raise ApprovalRequired(approval["id"], {"draft_id": draft_id})
        elif not decision.allowed:
            from app.core.errors import forbidden

            raise forbidden(required="EMAIL.SEND")

        # Send through the provider adapter only.
        self._adapter.require_connected()
        result = await self._adapter.execute("send", {
            "to": draft["to_addr"], "subject": draft["subject"], "body": draft["body"],
        })
        if not await self._adapter.verify(result):
            await self._audit.log("EMAIL_SEND", principal, "FAILURE", "email_draft", draft_id,
                                  details={"to": draft["to_addr"], "reason": "verification failed"})
            from app.core.errors import execution_failed

            raise execution_failed("Email send failed verification", {"draft_id": draft_id})
        sent_id = new_id("msg")
        await self._db.email.create_sent_message(sent_id, draft["to_addr"], draft["subject"], draft["body"],
                                                 result.get("external_id"))
        await self._db.email.update_draft(draft_id, "SENT", approval_id=approval_id, sent_message_id=sent_id)
        await self._audit.log("EMAIL_SEND", principal, "SUCCESS", "email_draft", draft_id,
                              details={"to": draft["to_addr"], "idempotency_key": idempotency_key})
        return {"message_id": sent_id, "draft_id": draft_id, "status": "SENT"}


def _draft_view(draft: dict) -> dict:
    """Contract EmailDraft shape: DB to_addr surfaces as `to`; subject/body non-null."""
    out = dict(draft)
    out["to"] = out.get("to_addr")
    out["subject"] = out.get("subject") or ""
    out["body"] = out.get("body") or ""
    return out
