"""Audit Service — INSERT-only. The DB layer (trigger + grants) enforces immutability;
this service simply never attempts UPDATE/DELETE."""
from __future__ import annotations

from app.core import context
from app.core.security import Principal


class AuditService:
    def __init__(self, db) -> None:
        self._db = db

    async def log(self, action: str, principal: Principal | None, result: str = "SUCCESS",
                  target_type: str | None = None, target_id: str | None = None,
                  project_id: str | None = None, execution_id: str | None = None,
                  command_id: str | None = None, details: dict | None = None) -> int:
        actor_type = "user"
        actor = principal.user_id if principal else "system"
        if principal is not None and principal.is_agent:
            actor_type, actor = "agent", "agent"
        audit_id = await self._db.audit.log(
            actor_type=actor_type,
            actor=actor,
            action=action,
            result=result,
            target_type=target_type,
            target_id=target_id,
            project_id=project_id,
            execution_id=execution_id,
            command_id=command_id,
            request_id=context.request_id_var.get(),
            details=details,
        )
        context.log_event("audit", {"action": action, "target": target_id, "result": result, "audit_id": audit_id})
        return audit_id

    async def list(self, project_id, action, actor, result, from_, to, page: int, size: int):
        """Contract AuditRecord shape: DB `occurred_at` surfaces as `timestamp` (RFC3339 Z)."""
        rows, total = await self._db.audit.list(project_id, action, actor, result, from_, to, page, size)
        out = []
        for r in rows:
            ts = r.pop("occurred_at")
            r["timestamp"] = ts.isoformat().replace("+00:00", "Z")
            out.append(r)
        return out, total
