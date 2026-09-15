"""Agent Service — orchestrates the contract pipeline (rule #30) end to end:

command -> Brain(LLM plan, validated against the Tool Registry) ->
Permission Engine (per step) -> Action Executor (internal services or
integration adapters ONLY) -> Verification Engine (independent re-check) ->
LLM summary (best-effort) -> conversation message + Audit Log.

The LLM never calls a sensitive service directly; it only proposes plans,
which are validated and permission-gated before anything executes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agent.brain import AgentBrain
from app.agent.permission import Decision
from app.agent.tools import Tool, lookup
from app.core import context
from app.core.config import Settings
from app.core.errors import AppError, conflict, forbidden, not_found, tool_unavailable, validation_error
from app.core.security import Principal
from app.generated import models
from app.services.approval_service import ApprovalRequired, ApprovalService
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id


class AgentService:
    def __init__(self, db, settings: Settings, brain: AgentBrain, permission_engine,
                 verification_engine, audit: AuditService, events: EventBus,
                 approvals: ApprovalService, adapters: dict, incidents, tasks,
                 email, whatsapp, github, memory, automations) -> None:
        self._db = db
        self._settings = settings
        self._brain = brain
        self._perms = permission_engine
        self._verify = verification_engine
        self._audit = audit
        self._events = events
        self._approvals = approvals
        self._adapters = adapters
        self._incidents = incidents
        self._tasks = tasks
        self._email = email
        self._whatsapp = whatsapp
        self._github = github
        self._memory = memory
        self._automations = automations

    # ── POST /agent/command ────────────────────────────────────────────────
    async def command(self, principal: Principal, command: str, source: str,
                      conversation_id: str | None, autonomous: bool) -> dict:
        decision = await self._brain.plan(principal, command, conversation_id)
        intent, steps = decision["intent"], decision["steps"]
        conv_id = conversation_id or new_id("conv")
        await self._db.conversations.get_or_create(conv_id, principal.user_id)
        cmd_id = new_id("cmd")
        plan = [{"task": s["task"], "status": "PENDING", "detail": s["args"]} for s in steps]
        await self._db.commands.create(cmd_id, conv_id, principal.user_id, source, command,
                                       intent, autonomous, plan)
        await self._db.conversations.add_message(conv_id, "user", command, cmd_id)
        await self._audit.log("COMMAND_RECEIVED", principal, "SUCCESS", "command", cmd_id,
                              details={"intent": intent, "steps": [s["task"] for s in steps]})
        return {"command_id": cmd_id, "intent": intent,
                "status": models.AgentStatus.PLANNED.value, "plan": plan}

    # ── POST /agent/execute ────────────────────────────────────────────────
    async def execute(self, principal: Principal, command_id: str, approval_id: str | None) -> dict:
        cmd = await self._db.commands.get(command_id)
        if not cmd:
            raise not_found("command")
        if cmd["status"] not in ("PLANNED", "WAITING_APPROVAL"):
            raise conflict(f"Command is not in PLANNED or WAITING_APPROVAL state",
                           {"command_id": command_id, "status": cmd["status"]})

        granted_approval_id: str | None = None
        if principal.is_agent:
            # Agent cannot bypass the approval gate.
            if approval_id:
                approval = await self._approvals.get(approval_id)
                if approval["status"] != "APPROVED" or approval["type"] != "AGENT_EXECUTION" \
                        or approval["payload"].get("command_id") != command_id:
                    raise conflict("Approval does not grant this execution", {"approval_id": approval_id})
                granted_approval_id = approval_id
            else:
                approval = await self._approvals.request("AGENT_EXECUTION", {"command_id": command_id},
                                                         requested_by="agent",
                                                         reason="Agent plan execution requires human approval")
                await self._db.commands.update_status(command_id, "WAITING_APPROVAL")
                await self._audit.log("EXECUTION_BLOCKED_APPROVAL", principal, "DENIED", "command", command_id,
                                      details={"approval_id": approval["id"]})
                raise ApprovalRequired(approval["id"], {"command_id": command_id})

        plan = [dict(s) for s in (cmd["plan"] or [])]
        exec_id = new_id("exec")
        context.execution_id_var.set(exec_id)
        await self._db.executions.create(exec_id, command_id, granted_approval_id)
        await self._db.commands.update_status(command_id, "RUNNING")
        conv_id = cmd["conversation_id"]

        try:
            for idx, step in enumerate(plan):
                tool: Tool | None = lookup(step["task"])
                if tool is None:
                    step.update(status="FAILED", result={"error": f"unknown tool {step['task']}"})
                    raise _StepFailure(models.ErrorCode.EXECUTION_FAILED, f"Unknown planner task {step['task']}")
                decision = await self._perms.check(principal, tool.resource, tool.action,
                                                   (step.get("detail") or {}).get("project_id"))
                if not decision.allowed and decision.requires_approval and granted_approval_id:
                    decision = Decision(True, "approved", False)
                if not decision.allowed:
                    if decision.requires_approval:
                        step.update(status="WAITING_APPROVAL")
                        await self._db.commands.update_plan(command_id, plan)
                        approval = await self._approvals.request(
                            "AGENT_EXECUTION",
                            {"command_id": command_id, "step": step["task"],
                             "project_id": (step.get("detail") or {}).get("project_id")},
                            requested_by=principal.user_id if principal.is_owner else "agent",
                            reason=f"Step {step['task']} requires human approval")
                        await self._db.commands.update_status(command_id, "WAITING_APPROVAL")
                        await self._db.executions.set_status(exec_id, "WAITING_APPROVAL")
                        await self._audit.log("EXECUTION_BLOCKED_APPROVAL", principal, "DENIED", "command", command_id,
                                              execution_id=exec_id,
                                              details={"approval_id": approval["id"], "step": step["task"]})
                        raise ApprovalRequired(approval["id"], {"command_id": command_id, "step": step["task"]})
                    step.update(status="FAILED", result={"error": decision.reason})
                    await self._db.commands.update_plan(command_id, plan)
                    await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.PERMISSION_DENIED.value,
                                                     decision.reason or "Permission denied", None, {"plan": plan})
                    await self._db.commands.update_status(command_id, "BLOCKED")
                    await self._audit.log("EXECUTION_PERMISSION_DENIED", principal, "DENIED", "command", command_id,
                                          execution_id=exec_id, details={"step": step["task"]})
                    raise forbidden(required=f"{tool.resource}.{tool.action}")

                try:
                    result = await self._run_tool(tool, step, principal, exec_id, idx, granted_approval_id)
                    ok = True
                    if tool.provider and tool.provider in self._adapters:
                        ok = await self._adapters[tool.provider].verify(result)
                    step.update(status="COMPLETED" if ok else "FAILED", result=result)
                    if not ok:
                        await self._db.commands.update_plan(command_id, plan)
                        await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.VERIFICATION_FAILED.value,
                                                         f"Step {step['task']} failed adapter verification", None,
                                                         {"plan": plan})
                        await self._db.commands.update_status(command_id, "FAILED")
                        await self._audit.log("EXECUTION_STEP_FAILED", principal, "FAILURE", "command", command_id,
                                              execution_id=exec_id, details={"step": step["task"]})
                        await self._reply(principal, conv_id, command_id, cmd["command_text"], plan)
                        return self._exec_data(exec_id, command_id, "FAILED")
                except ApprovalRequired:
                    # A tool requested human approval mid-plan (e.g. high-impact
                    # email): the approval record exists — pause honestly (409).
                    await self._db.commands.update_plan(command_id, plan)
                    await self._db.commands.update_status(command_id, "WAITING_APPROVAL")
                    await self._db.executions.set_status(exec_id, "WAITING_APPROVAL")
                    raise
                except AppError as e:
                    step.update(status="FAILED", result={"error": e.message, "code": e.code.value})
                    await self._db.commands.update_plan(command_id, plan)
                    # External unavailability is an execution OUTCOME, not an API failure:
                    # the execution is recorded FAILED with the fixed error code.
                    await self._db.executions.finish(exec_id, "FAILED", e.code.value, e.message, None, {"plan": plan})
                    await self._db.commands.update_status(command_id, "FAILED")
                    await self._audit.log("EXECUTION_STEP_FAILED", principal, "FAILURE", "command", command_id,
                                          execution_id=exec_id, details={"step": step["task"], "code": e.code.value})
                    await self._reply(principal, conv_id, command_id, cmd["command_text"], plan)
                    return self._exec_data(exec_id, command_id, "FAILED")
                await self._db.commands.update_plan(command_id, plan)

            # All steps completed → independent verification pass.
            ver_id, ok, checks = await self._verify.verify_execution(exec_id, plan)
            if ok:
                await self._db.executions.finish(exec_id, "COMPLETED", None, None, ver_id,
                                                 {"plan": plan, "checks": checks})
                await self._db.commands.update_status(command_id, "COMPLETED")
                await self._audit.log("EXECUTION_COMPLETED", principal, "SUCCESS", "command", command_id,
                                      execution_id=exec_id)
                await self._reply(principal, conv_id, command_id, cmd["command_text"], plan)
                return self._exec_data(exec_id, command_id, "COMPLETED")
            await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.VERIFICATION_FAILED.value,
                                             "Post-execution verification failed", ver_id,
                                             {"plan": plan, "checks": checks})
            await self._db.commands.update_status(command_id, "FAILED")
            await self._audit.log("EXECUTION_VERIFICATION_FAILED", principal, "FAILURE", "command", command_id,
                                  execution_id=exec_id)
            await self._reply(principal, conv_id, command_id, cmd["command_text"], plan)
            return self._exec_data(exec_id, command_id, "FAILED")
        except _StepFailure as sf:
            await self._db.executions.finish(exec_id, "FAILED", sf.code.value, sf.message, None, {"plan": plan})
            await self._db.commands.update_status(command_id, "FAILED")
            await self._audit.log("EXECUTION_FAILED", principal, "FAILURE", "command", command_id, execution_id=exec_id)
            return self._exec_data(exec_id, command_id, "FAILED")

    @staticmethod
    def _exec_data(exec_id: str, command_id: str, status: str) -> dict:
        return {"execution_id": exec_id, "command_id": command_id, "status": status}

    # ── agent reply (conversation memory for the next turn) ───────────────
    async def _reply(self, principal: Principal, conv_id: str | None, command_id: str,
                     command_text: str, plan: list[dict]) -> None:
        if not conv_id:
            return
        summary = await self._brain.summarize(principal, command_text, plan, plan)
        try:
            await self._db.conversations.add_message(conv_id, "agent", summary, command_id)
        except Exception:  # noqa: BLE001 — a failed reply must not hide the execution outcome
            context.log_event("agent.reply_failed", {"command_id": command_id})

    # ── tool execution ─────────────────────────────────────────────────────
    async def _run_tool(self, tool: Tool, step: dict, principal: Principal,
                        exec_id: str, idx: int, granted_approval_id: str | None) -> dict:
        detail = step.get("detail") or {}
        # Deterministic per-step idempotency key: unique per execution, stable
        # across the (single) attempt, never reused by another execution.
        step_key = f"agent:{exec_id}:{idx}"

        name = tool.name
        # Direct adapter path: the only tools without a service wrapper
        # (github read snapshots). Everything else goes through the services,
        # which carry their own permission re-checks, approval matching,
        # content policy, idempotency persistence, audit and adapter verify.
        if name == "RESEARCH_FETCH":
            # declared by the contract, no provider in v1 core — honest state
            raise tool_unavailable("RESEARCH_FETCH")
        if name in ("CHECK_OPEN_ISSUES", "CHECK_DEPLOYMENT"):
            adapter = self._adapters.get("github")
            if adapter is None:
                raise tool_unavailable(name)
            repo = await self._resolve_repo(detail.get("project_id"))
            if repo is None:
                raise tool_unavailable(name)
            return await adapter.execute("repo_status", {"repository": repo["full_name"], **detail})

        if name == "GET_PROJECT_STATUS":
            project_id = detail.get("project_id")
            if not project_id:
                raise tool_unavailable("GET_PROJECT_STATUS")
            row = await self._db.projects.detail(project_id)
            if row is None:
                raise not_found("project")
            return {"id": row["id"], "name": row["name"], "status": row["status"], "version": row["version"]}
        if name == "LIST_PROJECTS":
            rows, _ = await self._db.projects.list(1, 50)
            return {"projects": [{"id": p["id"], "name": p["name"], "status": p["status"], "priority": p["priority"]}
                                 for p in rows]}
        if name == "CREATE_TASK":
            task = await self._tasks.create(principal, detail["project_id"], detail["title"],
                                            detail.get("priority", "P2"),
                                            description=detail.get("description"),
                                            due_at=self._parse_dt(detail.get("due_at")))
            return {"task_id": task["id"], "title": task["title"], "status": task["status"]}
        if name == "LIST_TASKS":
            rows, total = await self._tasks.list(detail.get("project_id"), detail.get("status"),
                                                 None, None, None, 1, 20)
            return {"count": total, "recent": [{"id": t["id"], "title": t["title"], "status": t["status"]}
                                               for t in rows[:5]]}
        if name == "OPEN_INCIDENT":
            project_id = detail.get("project_id")
            project = await self._db.projects.get(project_id) if project_id else None
            incident = await self._incidents.create(
                principal, "CRITICAL", project["name"] if project else "agent",
                detail.get("issue") or "Incident raised by agent", detail.get("impact") or "Impact under assessment")
            return {"incident_id": incident["id"]}
        if name == "SEND_WHATSAPP":
            result = await self._whatsapp.send(principal, detail["to"], detail["message"], step_key)
            return result
        if name == "LIST_WA_INBOX":
            rows, total = await self._db.whatsapp.list_inbound(1, 5)
            return {"count": total, "recent": [r["sender"] for r in rows]}
        if name == "DRAFT_EMAIL":
            draft = await self._email.create_draft(principal, None, detail["to"], detail["subject"], detail["body"])
            return {"draft_id": draft["id"], "to": draft["to"], "subject": draft["subject"]}
        if name == "SEND_EMAIL":
            approval_id = await self._matching_email_approval(granted_approval_id, detail["draft_id"])
            result = await self._email.send(principal, detail["draft_id"], step_key, approval_id)
            return result
        if name == "LIST_EMAILS":
            rows, total = await self._email.list_messages(detail.get("folder"), None, 1,
                                                          min(detail.get("limit") or 10, 50))
            return {"count": total, "recent": [{"id": r["id"], "subject": r["subject"]} for r in rows[:5]]}
        if name == "CREATE_GITHUB_ISSUE":
            result = await self._github.create_issue(principal, detail["repository"], detail["title"],
                                                     detail.get("body"), detail.get("priority"), detail.get("labels"))
            return result
        if name == "SAVE_MEMORY":
            row = await self._memory.write(principal, detail["type"], detail["key"], detail["value"],
                                           detail.get("project_id"), source="agent")
            return {"memory_id": row["id"], "type": row["type"], "key": row["key"]}
        if name == "SEARCH_MEMORY":
            rows, total = await self._memory.list(detail.get("type"), None, detail.get("key"), 1, 20)
            return {"count": total, "memories": [{"id": m["id"], "type": m["type"], "key": m["key"],
                                                  "value": m["value"]} for m in rows]}
        if name == "CREATE_AUTOMATION":
            row = await self._automations.create(principal, detail["name"], detail["trigger"], detail["action"],
                                                 description=detail.get("description"))
            return {"automation_id": row["id"], "next_run_at": str(row["next_run_at"]) if row["next_run_at"] else None}
        if name == "GET_SYSTEM_STATUS":
            from app.services.system_service import SystemService

            status_service = SystemService(self._db, self._adapters, lambda: True)
            return await status_service.status()
        raise tool_unavailable(tool.name)

    async def _resolve_repo(self, project_id: str | None) -> dict | None:
        if not project_id:
            return None
        return await self._db.fetchone(
            "SELECT r.* FROM github_repositories r WHERE r.project_id = %s ORDER BY r.created_at LIMIT 1", project_id)

    async def _matching_email_approval(self, approval_id: str | None, draft_id: str) -> str | None:
        """Pass the granted approval to the email service only when it was
        requested for THIS draft (exact-payload semantics live in the service)."""
        if not approval_id:
            return None
        approval = await self._approvals.get(approval_id)
        if (approval and approval["status"] == "APPROVED" and approval["type"] == "EMAIL_SEND"
                and (approval["payload"] or {}).get("draft_id") == draft_id):
            return approval_id
        return None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as e:
            raise validation_error({"field": "due_at", "reason": "must be RFC3339"}) from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


class _StepFailure(AppError):
    def __init__(self, code, message):
        super().__init__(code, message)
