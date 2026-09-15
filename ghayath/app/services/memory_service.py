"""Memory service (contract §8): upsert semantics, typed, sourced, audited."""
from __future__ import annotations

from datetime import datetime

from app.core.errors import AppError, forbidden, not_found
from app.core.security import Principal
from app.generated import models
from app.services.audit_service import AuditService
from app.services.ids import new_id


class MemoryService:
    def __init__(self, db, audit: AuditService) -> None:
        self._db = db
        self._audit = audit

    async def write(self, principal: Principal, type: str, key: str, value, project_id: str | None,
                    source: str, confidence: float | None = None, expires_at: datetime | None = None) -> dict:
        row = await self._db.memory.upsert(type, key, value, project_id, source, confidence, expires_at)
        await self._audit.log("MEMORY_WRITE", principal, "SUCCESS", "memory", row["id"],
                              project_id=project_id, details={"type": type, "key": key, "source": source})
        return row

    async def list(self, type: str | None, project_id: str | None, key: str | None, page: int, size: int):
        return await self._db.memory.list(type, project_id, key, page, size)

    async def delete(self, principal: Principal, memory_id: str) -> dict:
        # MEMORY.DELETE: only the OWNER holds it; the agent cannot delete memory.
        if not principal.is_owner:
            raise forbidden(required="MEMORY.DELETE")
        row = await self._db.memory.get(memory_id)
        if not row:
            raise not_found("memory record")
        deleted = await self._db.memory.delete(memory_id)
        await self._audit.log("MEMORY_DELETE", principal, "SUCCESS", "memory", memory_id,
                              project_id=row.get("project_id"),
                              details={"removed_record": {"type": row["type"], "key": row["key"], "value": row["value"]}})
        return {"memory_id": memory_id, "deleted": True}
