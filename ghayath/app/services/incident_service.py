"""Incident service (contract §22): open incident → audit + event + CRITICAL notification."""
from __future__ import annotations

from app.core.errors import conflict, not_found
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"INVESTIGATING", "RESOLVED"},
    "INVESTIGATING": {"MITIGATED", "RESOLVED"},
    "MITIGATED": {"RESOLVED", "INVESTIGATING"},
    "RESOLVED": {"CLOSED"},
    "CLOSED": set(),
}


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

    async def transition(self, principal: Principal, incident_id: str, status: str,
                         resolution_note: str | None = None) -> dict:
        current = await self._db.incidents.get(incident_id)
        if current is None:
            raise not_found("incident")
        old_status = current["status"]
        if status == old_status or status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
            raise conflict(f"Transition {old_status}→{status} is not allowed", {
                "incident_id": incident_id, "from": old_status, "to": status,
            })
        updated = await self._db.incidents.update_status(incident_id, status)
        if updated is None:
            raise not_found("incident")
        details: dict[str, str] = {"from": old_status, "to": status}
        if resolution_note is not None:
            # The DDL intentionally has no note column. Keep the note only in
            # the immutable audit record as required by the v1.1 contract.
            details["resolution_note"] = resolution_note
        await self._audit.log("PATCH_INCIDENT", principal, "SUCCESS", "incident", incident_id,
                              details=details)
        return updated
