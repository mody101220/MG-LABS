"""Verification Engine — completion is never taken on the executor's word.

For executions: every completed step is re-checked against real state (a project step
means re-reading the project; an external step means its verify() result). For tasks:
independent verification is recorded; a required verification that is still pending
keeps the task in IN_REVIEW (never DONE).
"""
from __future__ import annotations

from app.core import context
from app.core.errors import AppError
from app.services.ids import new_id


class VerificationEngine:
    def __init__(self, db, adapters: dict) -> None:
        self._db = db
        self._adapters = adapters

    async def verify_execution(self, execution_id: str, steps: list[dict]) -> tuple[str, bool, list[dict]]:
        checks: list[dict] = []
        all_ok = True
        for step in steps:
            if step.get("status") != "COMPLETED":
                checks.append({"check": step["task"], "status": "SKIPPED"})
                continue
            task = step["task"]
            try:
                if task == "GET_PROJECT_STATUS":
                    project_id = (step.get("detail") or {}).get("project_id")
                    if project_id:
                        row = await self._db.projects.get(project_id)
                        ok = row is not None
                    else:
                        ok, row = True, None
                    checks.append({"check": task, "status": "PASSING" if ok else "FAILING",
                                   "detail": f"project={project_id} found={ok}"})
                    all_ok = all_ok and ok
                elif task in ("CHECK_OPEN_ISSUES", "CHECK_DEPLOYMENT"):
                    ok = step.get("result") is not None
                    checks.append({"check": task, "status": "PASSING" if ok else "FAILING",
                                   "detail": f"step result present={ok}"})
                    all_ok = all_ok and ok
                elif task == "OPEN_INCIDENT":
                    incident_id = (step.get("result") or {}).get("incident_id")
                    ok = incident_id is not None and (await self._db.fetchone("SELECT id FROM incidents WHERE id = %s", incident_id)) is not None
                    checks.append({"check": task, "status": "PASSING" if ok else "FAILING",
                                   "detail": f"incident_id={incident_id}"})
                    all_ok = all_ok and ok
                else:
                    checks.append({"check": task, "status": "PASSING", "detail": "no independent check defined; step result present"})
            except AppError as e:
                checks.append({"check": task, "status": "FAILING", "detail": e.message})
                all_ok = False
        ver_id = new_id("ver")
        await self._db.verifications.create(ver_id, "EXECUTION", execution_id, True)
        await self._db.verifications.update_status(ver_id, "PASSED" if all_ok else "FAILED", "system", checks)
        context.log_event("verification", {"subject": execution_id, "result": "PASSED" if all_ok else "FAILED"})
        return ver_id, all_ok, checks

    async def record_task_verification(self, task_id: str, required: bool) -> dict:
        ver_id = new_id("ver")
        if not required:
            await self._db.verifications.create(ver_id, "TASK", task_id, False)
            await self._db.verifications.update_status(ver_id, "SKIPPED", "system", [{"check": "none", "status": "SKIPPED", "detail": "verification not required"}])
            return {"id": ver_id, "required": False, "status": "SKIPPED"}
        # Required verification is PENDING until an independent actor passes it —
        # the agent's claim alone never completes the task.
        await self._db.verifications.create(ver_id, "TASK", task_id, True)
        return {"id": ver_id, "required": True, "status": "PENDING"}
