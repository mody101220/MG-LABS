"""Monitoring service (contract §15): active checks against BUILD, DEPLOYMENT, API,
DATABASE, UPTIME, ERRORS, SECURITY, CI/CD.

Checks that need a provider use the adapter (explicit INTEGRATION_OFFLINE when not
connected). Checks that are local (DATABASE, API probe) run for real."""
from __future__ import annotations

import httpx

from app.core import context
from app.core.errors import not_found
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.ids import new_id
from psycopg.rows import dict_row

_ALL_CHECKS = ["BUILD", "DEPLOYMENT", "API", "DATABASE", "UPTIME", "ERRORS", "SECURITY", "CICD"]
_PROVIDER_CHECKS = {"BUILD", "DEPLOYMENT", "ERRORS", "SECURITY", "CICD"}


class MonitoringService:
    def __init__(self, db, adapters, audit: AuditService) -> None:
        self._db = db
        self._adapters = adapters
        self._audit = audit

    async def check(self, principal: Principal, target_type: str, target_id: str, checks: list[str] | None) -> dict:
        checks = checks or _ALL_CHECKS
        unknown = [c for c in checks if c not in _ALL_CHECKS]
        if unknown:
            from app.core.errors import validation_error

            raise validation_error({"field": "checks", "unknown": unknown})

        project = None
        repo = None
        if target_type == "PROJECT":
            project = await self._db.projects.get(target_id)
            if not project:
                raise not_found("project")
            repo = await self._db.fetchone(
                "SELECT r.* FROM github_repositories r WHERE r.project_id = %s LIMIT 1", target_id)
        elif target_type == "REPOSITORY":
            repo = await self._db.github.get(target_id)
            if not repo:
                raise not_found("repository")
        elif target_type in ("SERVICE", "URL"):
            project = None

        results = []
        for name in checks:
            status, detail = await self._run_check(name, target_type, target_id, project, repo)
            results.append({"check": name, "status": status, "detail": detail})

        failing = [r for r in results if r["status"] == "FAILING"]
        warning = [r for r in results if r["status"] == "WARNING"]
        if failing:
            overall = "CRITICAL" if any(r["check"] in ("DEPLOYMENT", "UPTIME", "API") for r in failing) else "DEGRADED"
        elif warning or any(r["status"] == "UNKNOWN" for r in results):
            overall = "DEGRADED" if warning else "UNKNOWN"
        else:
            overall = "HEALTHY"

        check_id = new_id("chk")
        await self._audit.log("MONITORING_CHECK", principal, "SUCCESS", "monitoring_check", check_id,
                              project_id=project["id"] if project else None,
                              details={"target": target_id, "checks": results, "status": overall})
        return {
            "check_id": check_id,
            "target_type": target_type,
            "target_id": target_id,
            "status": overall,
            "results": results,
            "started_at": context.now_utc().isoformat().replace("+00:00", "Z"),
            "duration_ms": 0,
        }

    async def _run_check(self, name: str, target_type: str, target_id: str,
                         project: dict | None, repo: dict | None) -> tuple[str, str | None]:
        if name == "DATABASE":
            try:
                async with self._db.pool.connection() as conn:
                    conn.row_factory = dict_row
                    await conn.execute("SELECT 1")
                return "PASSING", "SELECT 1 ok"
            except Exception:
                return "FAILING", "database unreachable"
        if name in ("API", "UPTIME") and project and project.get("deployment_url"):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(project["deployment_url"])
                return ("PASSING" if resp.status_code < 500 else "FAILING"), f"HTTP {resp.status_code}"
            except Exception as e:
                return "FAILING", f"unreachable: {type(e).__name__}"
        if name in _PROVIDER_CHECKS:
            if repo is None:
                return "UNKNOWN", "no repository linked"
            adapter = self._adapters.get("github")
            if adapter is None or not adapter._configured():
                # Explicit, documented state — never simulated.
                return "UNKNOWN", "github integration not connected"
            try:
                live = await adapter.execute("repo_status", {"repository": repo["full_name"]})
                mapping = {
                    "BUILD": ("build", "PASSING", "FAILING"),
                    "DEPLOYMENT": ("deployment", "HEALTHY", "DOWN"),
                    "ERRORS": ("open_issues", None, None),
                    "SECURITY": ("security_alerts", None, None),
                    "CICD": ("build", "PASSING", "FAILING"),
                }
                key, good, bad = mapping[name]
                value = live.get(key)
                if key in ("open_issues", "security_alerts"):
                    return ("WARNING" if value else "PASSING"), f"{key}={value}"
                if value == good:
                    return "PASSING", f"{key}={value}"
                if value == bad:
                    return "FAILING", f"{key}={value}"
                return "UNKNOWN", f"{key}={value}"
            except Exception as e:
                return "FAILING", f"provider check failed: {e.message if hasattr(e, 'message') else type(e).__name__}"
        return "UNKNOWN", "no checker defined"
