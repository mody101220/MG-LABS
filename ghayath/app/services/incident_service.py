"""Incident service (contract §22): open incident → audit + event + CRITICAL notification."""
from __future__ import annotations

from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id


class IncidentService:
    def __init__(self, db, events: EventBus, audit: AuditService, notifications) -> None:
        self._db = db
        self._events = events
        self._audit = audit
        self._notifications = notifications

    async def create(self, principal: Principal, severity: str, system: str, issue: str,
                     impact: str | None, source: str = "agent") -> dict:
        incident = await self._db.incidents.create(new_id("inc"), severity, system, issue, impact, source)
        await self._audit.log("INCIDENT_CREATED", principal, "SUCCESS", "incident", incident["id"],
                              details={"severity": severity, "system": system})
        await self._events.publish("incident.created", "agent", None, severity,
                                   {"incident_id": incident["id"], "system": system})
        if severity in ("HIGH", "CRITICAL"):
            try:
                await self._notifications.create(principal, severity, None,
                                                 f"Incident: {system}", f"{issue} — {impact or 'impact under assessment'}",
                                                 None, {"incident_id": incident["id"]})
            except Exception:
                # Notification failure never blocks incident creation.
                pass
        return incident
