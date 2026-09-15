"""Task service (contract §7). Completion is verification-gated:
a task with required verification moves to IN_REVIEW and never becomes DONE on the
agent's word alone."""
from __future__ import annotations

from datetime import datetime

from app.core.errors import conflict, not_found, validation_error
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id

_VALID_CREATE_STATUSES = {"TODO", "IN_PROGRESS", "BLOCKED"}


class TaskService:
    def __init__(self, db, audit: AuditService, events: EventBus, verification_engine) -> None:
        self._db = db
        self._audit = audit
        self._events = events
        self._verify = verification_engine

    async def _with_verification(self, task: dict) -> dict:
        """Contract Task.verification (TaskVerification) — always present."""
        row = await self._db.fetchone(
            "SELECT * FROM verifications WHERE subject_type = 'TASK' AND subject_id = %s "
            "ORDER BY created_at DESC LIMIT 1", task["id"])
        if row is None:
            ver = {"required": False, "status": "NOT_REQUIRED", "verified_by": None, "verified_at": None}
        else:
            status = row["status"] if row["status"] != "SKIPPED" else "NOT_REQUIRED"
            ver = {
                "required": row["required"],
                "status": status,
                "verified_by": row["verified_by"],
                "verified_at": row["verified_at"].isoformat().replace("+00:00", "Z") if row["verified_at"] else None,
            }
        out = dict(task)
        out["verification"] = ver
        if out.get("due_at") is not None:
            out["due_at"] = out["due_at"].isoformat().replace("+00:00", "Z")
        if out.get("completed_at") is not None:
            out["completed_at"] = out["completed_at"].isoformat().replace("+00:00", "Z")
        if out.get("created_at") is not None:
            out["created_at"] = out["created_at"].isoformat().replace("+00:00", "Z")
        if out.get("updated_at") is not None:
            out["updated_at"] = out["updated_at"].isoformat().replace("+00:00", "Z")
        return out

    async def create(self, principal: Principal | None, project_id: str, title: str, priority: str,
                     status: str = "TODO", description: str | None = None, assignee: str | None = None,
                     due_at: datetime | None = None) -> dict:
        project = await self._db.projects.get(project_id)
        if not project:
            raise not_found("project")
        if status not in _VALID_CREATE_STATUSES:
            raise validation_error({"field": "status", "allowed": sorted(_VALID_CREATE_STATUSES)})
        task = await self._db.tasks.create(new_id("ts"), project_id, title, priority, status, description, assignee, due_at)
        await self._audit.log("TASK_CREATED", principal, "SUCCESS", "task", task["id"], project_id=project_id)
        await self._events.publish("task.created", "api", project_id, "INFO", {"task_id": task["id"]})
        return await self._with_verification(task)

    async def list(self, project_id: str | None, status: str | None, priority: str | None,
                   assignee: str | None, due_before: datetime | None, page: int, size: int):
        rows, total = await self._db.tasks.list(project_id, status, priority, assignee, due_before, page, size)
        return [await self._with_verification(t) for t in rows], total

    async def update(self, principal: Principal, task_id: str, fields: dict) -> dict:
        task = await self._db.tasks.get(task_id)
        if not task:
            raise not_found("task")
        if fields.get("status") == "DONE":
            # DONE is only reachable through the completion flow.
            raise validation_error({"field": "status", "reason": "use POST /tasks/{task_id}/complete (verification-gated)"})
        updated = await self._db.tasks.update(task_id, fields)
        if updated is None:
            raise conflict("Task state conflict")
        await self._audit.log("TASK_UPDATED", principal, "SUCCESS", "task", task_id,
                              project_id=task["project_id"], details={"fields": list(fields.keys())})
        return await self._with_verification(updated)

    async def create_for_automation(self, project_id: str | None, title: str | None, priority: str = "P2") -> dict:
        """Automation-driven task creation (scheduler path)."""
        if not project_id or not title:
            raise validation_error({"field": "action.params", "reason": "CREATE_TASK requires project_id and title"})
        principal = None
        return await self.create(principal, project_id, title, priority)

    async def complete(self, principal: Principal, task_id: str, verification_required: bool) -> dict:
        task = await self._db.tasks.get(task_id)
        if not task:
            raise not_found("task")
        if task["status"] == "DONE":
            raise conflict("Task is already completed", {"task_id": task_id})
        if task["status"] == "CANCELLED":
            raise conflict("Cancelled tasks cannot be completed", {"task_id": task_id})
        ver = await self._verify.record_task_verification(task_id, verification_required)
        if not verification_required:
            await self._db.tasks.mark_done(task_id)
            await self._audit.log("TASK_COMPLETED", principal, "SUCCESS", "task", task_id,
                                  project_id=task["project_id"],
                                  details={"verification": "not_required", "verification_id": ver["id"]})
            await self._events.publish("task.completed", "api", task["project_id"], "INFO", {"task_id": task_id})
            return {
                "task_id": task_id,
                "status": "DONE",
                "verification_status": "NOT_REQUIRED",
                "verification_id": ver["id"],
            }
        # Required: task enters IN_REVIEW with a PENDING verification.
        await self._db.tasks.update(task_id, {"status": "IN_REVIEW"})
        await self._audit.log("TASK_COMPLETION_CLAIMED", principal, "SUCCESS", "task", task_id,
                              project_id=task["project_id"],
                              details={"verification": "pending", "verification_id": ver["id"]})
        return {
            "task_id": task_id,
            "status": "IN_REVIEW",
            "verification_status": "PENDING",
            "verification_id": ver["id"],
        }
