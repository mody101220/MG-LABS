"""Permission Engine — the ONLY gate between the agent layer and any action.

Decision rules (three-role model + stored integration permission bits):
  VIEWER                      → deny (read-only role)
  OWNER                       → allow (owner authorizes)
  AGENT                       → allow iff the stored permission bit is true;
                                if false and the action is approval-eligible
                                (SEND/EXECUTE family) → requires_approval=True;
                                otherwise → deny.

The engine has NO grant path. The agent cannot grant itself permissions.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.security import Principal

# resource -> actions that are approval-eligible for the AGENT role (human gate).
_APPROVAL_ELIGIBLE: dict[str, set[str]] = {
    "WHATSAPP": {"SEND", "REPLY", "MEDIA"},
    "EMAIL": {"SEND", "DELETE"},
    "GITHUB": {"COMMIT", "MERGE", "PULL_REQUEST"},
    "AGENT": {"EXECUTE"},
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    requires_approval: bool = False


class PermissionEngine:
    def __init__(self, permission_state_getter):
        # getter: async (resource: str) -> dict[str, bool] of stored bits
        self._state = permission_state_getter

    async def check(self, principal: Principal, resource: str, action: str, target: str | None = None) -> Decision:
        if principal.is_viewer:
            return Decision(False, "VIEWER role is read-only", False)
        if principal.is_owner:
            return Decision(True, None, False)
        # AGENT: consult stored permission bits.
        bits = await self._state(resource)
        if bits.get(action, False):
            return Decision(True, None, False)
        if action in _APPROVAL_ELIGIBLE.get(resource, set()):
            return Decision(False, "Human approval required", True)
        return Decision(False, f"Required permission is missing ({resource}.{action})", False)
