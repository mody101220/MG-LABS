"""Daily Brief (contract §25) — assembled from real DB state; arrays are empty,
never null, when there is nothing."""
from __future__ import annotations

from datetime import timedelta

from app.core import context


class BriefService:
    def __init__(self, db) -> None:
        self._db = db

    async def daily(self, date_str: str | None) -> dict:
        today = context.now_utc().date()
        if date_str:
            from datetime import datetime

            today = datetime.strptime(date_str, "%Y-%m-%d").date()
        start = context.now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        if today != context.now_utc().date():
            start = start - timedelta(days=(context.now_utc().date() - today).days)

        def item(id_, title, summary=None, project_id=None, priority=None, due_at=None) -> dict:
            return {
                "id": id_, "title": title, "summary": summary, "project_id": project_id,
                "priority": priority,
                "due_at": due_at.isoformat().replace("+00:00", "Z") if due_at else None,
            }

        critical_incidents = await self._db.fetch(
            "SELECT * FROM incidents WHERE status = 'OPEN' AND severity IN ('CRITICAL','HIGH') ORDER BY created_at DESC")
        high_items = []
        urgent_emails = await self._db.fetch(
            "SELECT * FROM email_messages WHERE folder = 'inbox' AND priority IN ('HIGH','URGENT') AND is_read = FALSE ORDER BY received_at DESC LIMIT 10")
        for e in urgent_emails:
            high_items.append(item(e["id"], e["subject"] or "(no subject)",
                                   summary=e["summary"], priority=e["priority"]))

        open_tasks = await self._db.fetch(
            "SELECT t.*, p.name AS project_name FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
            "WHERE t.status NOT IN ('DONE','CANCELLED') ORDER BY t.due_at IS NULL, t.due_at, t.priority LIMIT 20")
        task_items = [
            item(t["id"], t["title"], project_id=t["project_id"], priority=t["priority"], due_at=t["due_at"])
            for t in open_tasks
        ]
        due_tasks = [t for t in open_tasks if t["due_at"] and t["due_at"].date() <= context.now_utc().date() + timedelta(days=1)]

        wa = await self._db.fetch(
            "SELECT * FROM whatsapp_messages WHERE direction='INBOUND' AND classification IN ('CLIENT','BUSINESS','URGENT') "
            "ORDER BY created_at DESC LIMIT 10")
        wa_items = [item(w["id"], f"WhatsApp from {w['sender']}", summary=(w["body"] or "")[:120],
                         priority="HIGH" if w["classification"] == "URGENT" else "NORMAL") for w in wa]

        projects = await self._db.fetch(
            "SELECT * FROM projects WHERE status = 'ACTIVE' ORDER BY last_activity_at DESC NULLS LAST LIMIT 10")
        project_items = [
            item(p["id"], p["name"], summary=p["next_action"], project_id=p["id"], priority=p["priority"])
            for p in projects
        ]

        actions = await self._db.fetch(
            "SELECT action, target_id, occurred_at FROM audit_logs WHERE actor_type = 'agent' AND result = 'SUCCESS' "
            "AND occurred_at >= %s ORDER BY id DESC LIMIT 20", start)
        actions_taken = [f"{a['action']}" + (f" → {a['target_id']}" if a["target_id"] else "") for a in actions]

        pending_approvals = await self._db.fetch(
            "SELECT * FROM approvals WHERE status = 'PENDING' ORDER BY requested_at LIMIT 5")
        next_actions = [f"Decide approval {a['id']} ({a['type']})" for a in pending_approvals]
        next_actions += [f"Follow up on task '{t['title']}'" for t in due_tasks[:5]]

        return {
            "date": today.isoformat(),
            "critical": [
                item(i["id"], f"[{i['severity']}] {i['system']}: {i['issue']}", project_id=None, priority=i["severity"])
                for i in critical_incidents
            ],
            "high": high_items,
            "tasks": task_items,
            "emails": [item(e["id"], e["subject"] or "(no subject)", summary=e["summary"], priority=e["priority"]) for e in urgent_emails],
            "whatsapp": wa_items,
            "projects": project_items,
            "deadlines": [item(t["id"], t["title"], project_id=t["project_id"], priority=t["priority"], due_at=t["due_at"]) for t in due_tasks],
            "actions_taken": actions_taken,
            "next_actions": next_actions,
        }
