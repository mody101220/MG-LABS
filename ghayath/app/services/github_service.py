"""GitHub service (contract §14). The adapter is the only path to the provider;
without credentials every call is explicitly INTEGRATION_OFFLINE (503)."""
from __future__ import annotations

from app.core.errors import not_found
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.ids import new_id


class GitHubService:
    def __init__(self, db, adapter, permission_engine, audit: AuditService) -> None:
        self._db = db
        self._adapter = adapter
        self._perms = permission_engine
        self._audit = audit

    async def list_repos(self, page: int, size: int):
        rows, total = await self._db.github.list_repos(page, size)
        return rows, total

    async def repo_status(self, principal: Principal, repo_id: str) -> dict:
        repo = await self._db.github.get(repo_id)
        if not repo:
            raise not_found("repository")
        decision = await self._perms.check(principal, "GITHUB", "READ", repo_id)
        if not decision.allowed:
            from app.core.errors import forbidden

            raise forbidden(required="GITHUB.READ")
        # Prefer the freshest snapshot; refresh live when the adapter is connected.
        if self._adapter._configured():
            live = await self._adapter.execute("repo_status", {"repository": repo["full_name"]})
            return {**live, "last_checked_at": _now_iso()}
        snap = await self._db.github.latest_snapshot(repo_id)
        if snap:
            return {
                "build": snap["build"], "tests": snap["tests"], "deployment": snap["deployment"],
                "open_issues": snap["open_issues"], "security_alerts": snap["security_alerts"],
                "last_checked_at": snap["checked_at"].isoformat().replace("+00:00", "Z"),
            }
        # No adapter and no stored status: honest UNKNOWN state, not a fabricated one.
        return {
            "build": "UNKNOWN", "tests": "UNKNOWN", "deployment": "UNKNOWN",
            "open_issues": 0, "security_alerts": 0,
            "last_checked_at": repo["last_checked_at"].isoformat().replace("+00:00", "Z") if repo["last_checked_at"] else None,
        }

    async def create_issue(self, principal: Principal, repository: str, title: str,
                           body: str | None, priority: str | None, labels: list[str] | None) -> dict:
        decision = await self._perms.check(principal, "GITHUB", "ISSUES", repository)
        if not decision.allowed and not decision.requires_approval:
            from app.core.errors import forbidden

            raise forbidden(required="GITHUB.ISSUES")
        repo = await self._db.github.get_by_full_name(repository)
        if not repo:
            raise not_found("repository")
        self._adapter.require_connected()
        result = await self._adapter.execute("create_issue", {
            "repository": repository, "title": title, "body": body,
        })
        issue = await self._db.github.create_issue(new_id("iss"), repo["id"], result["number"], title, body, priority, result.get("url"))
        await self._audit.log("GITHUB_ISSUE_CREATED", principal, "SUCCESS", "github_issue", issue["id"],
                              details={"repository": repository, "number": result["number"]})
        return {
            "issue_id": issue["id"], "number": result["number"], "repository": repository,
            "title": title, "priority": priority or "P2", "status": "OPEN", "url": result.get("url"),
        }


def _now_iso() -> str:
    from app.core import context

    return context.now_utc().isoformat().replace("+00:00", "Z")
