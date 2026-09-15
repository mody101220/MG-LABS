"""Permission Service — reads the stored permission state (integrations table) and
exposes it per the contract (GET /permissions). The AGENT role is bounded by these
bits; there is NO mutation path in v1 (the agent cannot grant itself permissions)."""
from __future__ import annotations

from app.core.security import Principal


class PermissionService:
    def __init__(self, db) -> None:
        self._db = db

    async def state_for_resource(self, resource: str) -> dict[str, bool]:
        row = await self._db.integrations.get(resource.lower())
        if not row:
            return {}
        perms = row["permissions"] or {}
        return {str(k): bool(v) for k, v in perms.items()}

    async def full_state(self) -> dict:
        rows = await self._db.integrations.list()
        state = {}
        for row in rows:
            state[row["id"].upper()] = {str(k): bool(v) for k, v in (row["permissions"] or {}).items()}
        return {
            "WHATSAPP": {**{"READ": False, "REPLY": False, "SEND": False, "MEDIA": False}, **state.get("WHATSAPP", {})},
            "EMAIL": {**{"READ": False, "DRAFT": False, "SEND": False, "DELETE": False}, **state.get("EMAIL", {})},
            "GITHUB": {**{"READ": False, "ISSUES": False, "PULL_REQUEST": False, "COMMIT": False, "MERGE": False},
                        **state.get("GITHUB", {})},
        }

    async def effective_for(self, principal: Principal) -> dict:
        """Contract GET /permissions: effective permissions for the principal."""
        base = await self.full_state()
        if principal.is_owner:
            # OWNER authorizes everything in the contract.
            return {group: {k: True for k in perms} for group, perms in base.items()}
        return base
