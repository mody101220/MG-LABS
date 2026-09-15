"""Notification service — severity routing per contract §20:

CRITICAL → PUSH + WHATSAPP | HIGH → PUSH | MEDIUM → DAILY_BRIEF | LOW → DASHBOARD
An explicit `channel` overrides routing. Delivery is real where a channel exists:
PUSH/DAILY_BRIEF/DASHBOARD are stored locally (dashboard reads the store); WHATSAPP
delivery goes through the adapter (explicitly fails when not connected/configured).
"""
from __future__ import annotations

from app.core.errors import integration_offline
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.ids import new_id

ROUTING: dict[str, list[str]] = {
    "CRITICAL": ["PUSH", "WHATSAPP"],
    "HIGH": ["PUSH"],
    "MEDIUM": ["DAILY_BRIEF"],
    "LOW": ["DASHBOARD"],
}


class NotificationService:
    def __init__(self, db, settings, whatsapp_adapter, audit: AuditService) -> None:
        self._db = db
        self._settings = settings
        self._wa = whatsapp_adapter
        self._audit = audit

    async def create(self, principal: Principal | None, severity: str, channel: str | None,
                     title: str, message: str, project_id: str | None, data: dict | None) -> dict:
        channels = [channel] if channel else list(ROUTING[severity])
        notf_id = new_id("notf")
        delivery: dict[str, str] = {}
        for ch in channels:
            if ch == "WHATSAPP":
                recipient = self._settings.notification_whatsapp_recipient
                if not recipient:
                    delivery[ch] = "FAILED"
                elif not self._wa._configured():
                    delivery[ch] = "FAILED"
                else:
                    try:
                        await self._wa.execute("send", {"recipient": recipient, "message": f"{title}: {message}"})
                        delivery[ch] = "SENT"
                    except Exception:
                        delivery[ch] = "FAILED"
            else:
                # Local channels: stored in the notifications table (dashboard/brief read them).
                delivery[ch] = "SENT"
        status = "SENT" if all(v == "SENT" for v in delivery.values()) else ("FAILED" if any(v == "FAILED" for v in delivery.values()) else "QUEUED")
        row = await self._db.notifications.create(notf_id, severity, channel, channels, title, message, project_id, status, data or {})
        await self._audit.log("NOTIFICATION_CREATED", principal, "SUCCESS", "notification", notf_id,
                              project_id=project_id, details={"severity": severity, "channels": channels, "delivery": delivery})
        return {
            "notification_id": row["id"],
            "routed_channels": channels,
            "status": status,
        }
