"""Automation scheduler — polls due SCHEDULE automations and runs EVENT-triggered
automations when the event bus publishes. Every run is audited."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core import context
from app.core.errors import AppError
from croniter import croniter


class Scheduler:
    def __init__(self, db, events, monitoring, tasks, notifications, email, automations_service) -> None:
        self._db = db
        self._events = events
        self._monitoring = monitoring
        self._tasks = tasks
        self._notifications = notifications
        self._email = email
        self._automations = automations_service
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ghayath-scheduler")
        context.log_event("scheduler.started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        context.log_event("scheduler.stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                due = await self._db.automations.due(context.now_utc())
                for auto in due:
                    await self._run(auto)
                await self._db.approvals.expire_stale()
                await self._db.idempotency.purge_expired()
            except Exception:
                context.log_event("scheduler.loop_error", level=110)
            await asyncio.sleep(15)

    async def run_event_automations(self, event: str, project_id: str | None) -> None:
        rows = await self._db.fetch(
            "SELECT * FROM automations WHERE enabled = TRUE AND trigger->>'type' = 'EVENT' AND trigger->>'event' = %s",
            event)
        for auto in rows:
            await self._run(auto)

    async def _run(self, auto: dict) -> None:
        action = auto["action"] or {}
        trigger = auto["trigger"] or {}
        await self._events.publish("automation.triggered", "scheduler",
                                   action.get("target"), "INFO", {"automation_id": auto["id"], "action": action.get("type")})
        status = "SUCCESS"
        try:
            if action.get("type") in ("PROJECT_MONITOR", "RUN_CHECK"):
                target_type = "PROJECT" if action.get("type") == "PROJECT_MONITOR" else (action.get("params") or {}).get("target_type", "PROJECT")
                await self._monitoring.check(None, target_type, action.get("target"), (action.get("params") or {}).get("checks"))
            elif action.get("type") == "CREATE_TASK":
                params = action.get("params") or {}
                await self._tasks.create_for_automation(params.get("project_id"), params.get("title"),
                                                        params.get("priority", "P2"))
            elif action.get("type") == "SEND_NOTIFICATION":
                params = action.get("params") or {}
                await self._notifications.create(None, params.get("severity", "LOW"), None,
                                                 params.get("title", "Automation"), params.get("message", ""),
                                                 action.get("target"), None)
            elif action.get("type") == "SEND_EMAIL_DRAFT":
                params = action.get("params") or {}
                await self._email.create_for_automation(params.get("to"), params.get("subject"), params.get("body"))
            else:
                status = "SKIPPED"
        except AppError as e:
            status = "FAILURE"
            context.log_event("automation.run_failed", {"automation_id": auto["id"], "code": e.code.value})
        except Exception:
            status = "FAILURE"
            context.log_event("automation.run_failed", {"automation_id": auto["id"]}, level=110)
        next_run = None
        if trigger.get("type") == "SCHEDULE" and trigger.get("cron") and auto["enabled"]:
            ts = croniter(trigger["cron"], context.now_utc()).get_next(float)
            next_run = datetime.fromtimestamp(ts, tz=timezone.utc)
        await self._db.automations.mark_run(auto["id"], status, next_run)
