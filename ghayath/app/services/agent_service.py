"""Agent Service — orchestrates the contract pipeline (rule #30):

LLM text → IntentEngine → Planner → ToolRouter → PermissionEngine →
ActionExecutor (internal tools or IntegrationAdapter only) → Verification → Audit.

The LLM/agent layer never calls an integration directly.
"""
from __future__ import annotations

from app.agent.permission import Decision
from app.agent.tools import Tool, lookup
from app.core import context
from app.core.config import Settings
from app.core.errors import AppError, conflict, forbidden, integration_offline, not_found, tool_unavailable
from app.core.security import Principal
from app.generated import models
from app.services.approval_service import ApprovalRequired, ApprovalService
from app.services.audit_service import AuditService
from app.services.events import EventBus
from app.services.ids import new_id


class AgentService:
    def __init__(self, db, settings: Settings, intent_engine, planner, permission_engine,
                 verification_engine, audit: AuditService, events: EventBus,
                 approvals: ApprovalService, adapters: dict, incidents) -> None:
        self._db = db
        self._settings = settings
        self._intent = intent_engine
        self._planner = planner
        self._perms = permission_engine
        self._verify = verification_engine
        self._audit = audit
        self._events = events
        self._approvals = approvals
        self._adapters = adapters
        self._incidents = incidents

    # ── POST /agent/command ────────────────────────────────────────────────
    async def command(self, principal: Principal, command: str, source: str,
                      conversation_id: str | None, autonomous: bool) -> dict:
        intent = await self._intent.classify(command, {"user_id": principal.user_id})
        plan = await self._planner.build_plan(intent, command)
        if conversation_id:
            conv_id = conversation_id
        else:
            conv_id = new_id("conv")
        await self._db.conversations.get_or_create(conv_id, principal.user_id)
        cmd_id = new_id("cmd")
        plan_serializable = [
            {"task": s["task"],
             "status": s["status"].value if isinstance(s["status"], models.StepStatus) else s["status"],
             "detail": s.get("detail") or {}}
            for s in plan
        ]
        await self._db.commands.create(cmd_id, conv_id, principal.user_id, source, command, intent, autonomous, plan_serializable)
        await self._db.conversations.add_message(conv_id, "user", command, cmd_id)
        await self._audit.log("COMMAND_RECEIVED", principal, "SUCCESS", "command", cmd_id, details={"intent": intent})
        return {
            "command_id": cmd_id,
            "intent": intent,
            "status": models.AgentStatus.PLANNED.value,
            "plan": [{"task": s["task"], "status": s["status"].value if isinstance(s["status"], models.StepStatus) else s["status"], "detail": s.get("detail") or {}} for s in plan],
        }

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

        try:
            for step in plan:
                tool: Tool | None = lookup(step["task"])
                if tool is None:
                    step.update(status="FAILED", result={"error": f"unknown tool {step['task']}"})
                    raise _StepFailure(models.ErrorCode.EXECUTION_FAILED, f"Unknown planner task {step['task']}")
                decision = await self._perms.check(principal, tool.resource, tool.action, (step.get("detail") or {}).get("project_id"))
                if not decision.allowed and decision.requires_approval and granted_approval_id:
                    decision = Decision(True, "approved", False)
                if not decision.allowed:
                    if decision.requires_approval:
                        step.update(status="WAITING_APPROVAL")
                        await self._db.commands.update_plan(command_id, plan)
                        approval = await self._approvals.request(
                            "AGENT_EXECUTION",
                            {"command_id": command_id, "step": step["task"], "project_id": (step.get("detail") or {}).get("project_id")},
                            requested_by=principal.user_id if principal.is_owner else "agent",
                            reason=f"Step {step['task']} requires human approval")
                        await self._db.commands.update_status(command_id, "WAITING_APPROVAL")
                        await self._db.executions.set_status(exec_id, "WAITING_APPROVAL")
                        await self._audit.log("EXECUTION_BLOCKED_APPROVAL", principal, "DENIED", "command", command_id,
                                              execution_id=exec_id, details={"approval_id": approval["id"], "step": step["task"]})
                        raise ApprovalRequired(approval["id"], {"command_id": command_id, "step": step["task"]})
                    step.update(status="FAILED", result={"error": decision.reason})
                    await self._db.commands.update_plan(command_id, plan)
                    await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.PERMISSION_DENIED.value,
                                                     decision.reason or "Permission denied", None,
                                                     {"plan": plan})
                    await self._db.commands.update_status(command_id, "BLOCKED")
                    await self._audit.log("EXECUTION_PERMISSION_DENIED", principal, "DENIED", "command", command_id,
                                          execution_id=exec_id, details={"step": step["task"]})
                    raise forbidden(required=f"{tool.resource}.{tool.action}")

                try:
                    result = await self._run_tool(tool, step, principal)
                    ok = True
                    if tool.provider and tool.provider in self._adapters:
                        ok = await self._adapters[tool.provider].verify(result)
                    step.update(status="COMPLETED" if ok else "FAILED", result=result)
                    if not ok:
                        await self._db.commands.update_plan(command_id, plan)
                        await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.VERIFICATION_FAILED.value,
                                                         f"Step {step['task']} failed adapter verification", None, {"plan": plan})
                        await self._db.commands.update_status(command_id, "FAILED")
                        await self._audit.log("EXECUTION_STEP_FAILED", principal, "FAILURE", "command", command_id,
                                              execution_id=exec_id, details={"step": step["task"]})
                        return self._exec_data(exec_id, command_id, "FAILED")
                except AppError as e:
                    step.update(status="FAILED", result={"error": e.message, "code": e.code.value})
                    await self._db.commands.update_plan(command_id, plan)
                    # External unavailability is an execution OUTCOME, not an API failure:
                    # the execution is recorded FAILED with the fixed error code.
                    await self._db.executions.finish(exec_id, "FAILED", e.code.value, e.message, None, {"plan": plan})
                    await self._db.commands.update_status(command_id, "FAILED")
                    await self._audit.log("EXECUTION_STEP_FAILED", principal, "FAILURE", "command", command_id,
                                          execution_id=exec_id, details={"step": step["task"], "code": e.code.value})
                    return self._exec_data(exec_id, command_id, "FAILED")
                await self._db.commands.update_plan(command_id, plan)

            # All steps completed → independent verification pass.
            ver_id, ok, checks = await self._verify.verify_execution(exec_id, plan)
            if ok:
                await self._db.executions.finish(exec_id, "COMPLETED", None, None, ver_id, {"plan": plan, "checks": checks})
                await self._db.commands.update_status(command_id, "COMPLETED")
                await self._audit.log("EXECUTION_COMPLETED", principal, "SUCCESS", "command", command_id,
                                      execution_id=exec_id)
                return self._exec_data(exec_id, command_id, "COMPLETED")
            await self._db.executions.finish(exec_id, "FAILED", models.ErrorCode.VERIFICATION_FAILED.value,
                                             "Post-execution verification failed", ver_id, {"plan": plan, "checks": checks})
            await self._db.commands.update_status(command_id, "FAILED")
            await self._audit.log("EXECUTION_VERIFICATION_FAILED", principal, "FAILURE", "command", command_id,
                                  execution_id=exec_id)
            return self._exec_data(exec_id, command_id, "FAILED")
        except _StepFailure as sf:
            await self._db.executions.finish(exec_id, "FAILED", sf.code.value, sf.message, None, {"plan": plan})
            await self._db.commands.update_status(command_id, "FAILED")
            await self._audit.log("EXECUTION_FAILED", principal, "FAILURE", "command", command_id, execution_id=exec_id)
            return self._exec_data(exec_id, command_id, "FAILED")

    @staticmethod
    def _exec_data(exec_id: str, command_id: str, status: str) -> dict:
        return {"execution_id": exec_id, "command_id": command_id, "status": status}

    # ── tool execution ─────────────────────────────────────────────────────
    async def _run_tool(self, tool: Tool, step: dict, principal: Principal) -> dict:
        detail = step.get("detail") or {}
        if tool.external:
            adapter = self._adapters.get(tool.provider or "")
            if adapter is None:
                raise tool_unavailable(tool.name)
            if tool.provider == "github":
                repo = await self._resolve_repo(detail.get("project_id"))
                if repo is None:
                    raise tool_unavailable(tool.name, details={"reason": "no repository linked to the target project"})
                return await adapter.execute(tool.provider_action or tool.name, {"repository": repo["full_name"], **detail})
            raise tool_unavailable(tool.name)
        # internal tools
        if tool.name == "GET_PROJECT_STATUS":
            project_id = detail.get("project_id")
            if not project_id:
                raise tool_unavailable(tool.name, details={"reason": "no project referenced in the command"})
            row = await self._db.projects.detail(project_id)
            if row is None:
                raise not_found("project")
            return {"id": row["id"], "name": row["name"], "status": row["status"], "version": row["version"]}
        if tool.name == "OPEN_INCIDENT":
            project_id = detail.get("project_id")
            project = await self._db.projects.get(project_id) if project_id else None
            incident = await self._incidents.create(
                principal, "CRITICAL", project["name"] if project else "unknown",
                detail.get("issue") or "Incident raised by agent", detail.get("impact") or "Impact under assessment")
            return {"incident_id": incident["id"]}
        if tool.name == "LIST_EMAILS":
            rows, total = await self._db.email.list_messages("inbox", None, 1, 5)
            return {"count": total, "recent": [r["subject"] for r in rows]}
        if tool.name == "LIST_WA_INBOX":
            rows, total = await self._db.whatsapp.list_inbound(1, 5)
            return {"count": total, "recent": [r["sender"] for r in rows]}
        if tool.name == "GET_SYSTEM_STATUS":
            from app.services.system_service import SystemService

            status_service = SystemService(self._db, self._adapters, getattr(self, "_scheduler_running", lambda: True))
            return await status_service.status()
        raise tool_unavailable(tool.name)

    async def _resolve_repo(self, project_id: str | None) -> dict | None:
        if not project_id:
            return None
        return await self._db.fetchone(
            "SELECT r.* FROM github_repositories r WHERE r.project_id = %s ORDER BY r.created_at LIMIT 1", project_id)


class _StepFailure(AppError):
    def __init__(self, code, message):
        super().__init__(code, message)
