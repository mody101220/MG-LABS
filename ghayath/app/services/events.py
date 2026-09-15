"""Internal event bus (contract §26). Events are persisted to the `events` table
and dispatch EVENT-triggered automations. Not exposed over HTTP in v1."""
from __future__ import annotations

from app.core import context
from app.services.ids import new_id


class EventBus:
    def __init__(self, db) -> None:
        self._db = db
        self._automation_runner = None  # set by the scheduler (async fn(event, project_id))

    def bind_automation_runner(self, runner) -> None:
        self._automation_runner = runner

    async def publish(self, event: str, source: str, project_id: str | None = None,
                      severity: str | None = None, payload: dict | None = None) -> dict:
        row = await self._db.events.publish(new_id("evt"), event, source, project_id, severity, payload or {})
        context.log_event("event.published", {"event": event, "source": source, "project_id": project_id, "event_id": row["id"]})
        if self._automation_runner is not None:
            try:
                await self._automation_runner(event, project_id)
            except Exception:
                context.log_event("event.automation_error", {"event": event}, level=__import__("logging").ERROR)
        return row
