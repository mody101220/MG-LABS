"""Automation service (contract §16): SCHEDULE (cron) / EVENT / MANUAL triggers.
Cron is validated with croniter; next_run_at is computed at creation and after each run."""
from __future__ import annotations

from datetime import datetime, timezone

from croniter import croniter

from app.core import context
from app.core.errors import AppError, not_found, validation_error
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id


class AutomationService:
    def __init__(self, db, audit: AuditService, events: EventBus) -> None:
        self._db = db
        self._audit = audit
        self._events = events

    def _validate_trigger(self, trigger: dict) -> None:
        t = trigger.get("type")
        if t not in ("SCHEDULE", "EVENT", "MANUAL"):
            raise validation_error({"field": "trigger.type"})
        if t == "SCHEDULE":
            cron = trigger.get("cron")
            if not cron or not croniter.is_valid(cron):
                raise validation_error({"field": "trigger.cron", "reason": "invalid 5-field cron expression"})
        if t == "EVENT" and not trigger.get("event"):
            raise validation_error({"field": "trigger.event"})

    def _next_run(self, trigger: dict) -> datetime | None:
        if trigger.get("type") != "SCHEDULE":
            return None
        cron = trigger.get("cron")
        if not cron:
            return None
        ts = croniter(cron, context.now_utc()).get_next(float)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _validate_action(self, action: dict) -> None:
        if action.get("type") not in ("PROJECT_MONITOR", "RUN_CHECK", "CREATE_TASK", "SEND_NOTIFICATION", "SEND_EMAIL_DRAFT"):
            raise validation_error({"field": "action.type"})

    async def create(self, principal: Principal, name: str, trigger: dict, action: dict,
                     enabled: bool = True, description: str | None = None) -> dict:
        self._validate_trigger(trigger)
        self._validate_action(action)
        automation = await self._db.automations.create(new_id("auto"), name, description, trigger, action,
                                                       enabled, self._next_run(trigger))
        await self._audit.log("AUTOMATION_CREATED", principal, "SUCCESS", "automation", automation["id"],
                              details={"trigger": trigger.get("type"), "action": action.get("type")})
        return automation

    async def update(self, principal: Principal, automation_id: str, name: str | None, description: str | None,
                     trigger: dict | None, action: dict | None, enabled: bool | None) -> dict:
        current = await self._db.automations.get(automation_id)
        if not current:
            raise not_found("automation")
        if trigger is not None:
            self._validate_trigger(trigger)
        if action is not None:
            self._validate_action(action)
        next_run = None
        if trigger is not None:
            next_run = self._next_run(trigger)
        elif enabled is True and current["enabled"] is False and current["trigger"].get("type") == "SCHEDULE":
            next_run = self._next_run(current["trigger"])
        updated = await self._db.automations.update(automation_id, name, description, trigger, action, enabled, next_run)
        await self._audit.log("AUTOMATION_UPDATED", principal, "SUCCESS", "automation", automation_id,
                              details={"fields": [k for k, v in dict(name=name, description=description,
                                                                     trigger=trigger, action=action, enabled=enabled).items() if v is not None]})
        return updated
