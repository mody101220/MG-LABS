"""Tool Router registry — the mapping between planner step names and executable tools.

Every tool declares the (resource, action) it requires, so the Permission Engine gates
it before execution. `external=True` tools route to an integration adapter — the only
path that touches a provider.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    resource: str
    action: str
    external: bool = False
    provider: str | None = None
    provider_action: str | None = None


TOOL_REGISTRY: dict[str, Tool] = {
    "GET_PROJECT_STATUS": Tool("GET_PROJECT_STATUS", "PROJECT", "READ"),
    "CHECK_OPEN_ISSUES": Tool("CHECK_OPEN_ISSUES", "GITHUB", "READ", external=True, provider="github", provider_action="repo_status"),
    "CHECK_DEPLOYMENT": Tool("CHECK_DEPLOYMENT", "GITHUB", "READ", external=True, provider="github", provider_action="repo_status"),
    "OPEN_INCIDENT": Tool("OPEN_INCIDENT", "INCIDENT", "CREATE"),
    "LIST_EMAILS": Tool("LIST_EMAILS", "EMAIL", "READ"),
    "LIST_WA_INBOX": Tool("LIST_WA_INBOX", "WHATSAPP", "READ"),
    "RESEARCH_FETCH": Tool("RESEARCH_FETCH", "RESEARCH", "FETCH", external=True, provider="research"),
    "GET_SYSTEM_STATUS": Tool("GET_SYSTEM_STATUS", "SYSTEM", "READ"),
}


def lookup(task: str) -> Tool | None:
    return TOOL_REGISTRY.get(task)
