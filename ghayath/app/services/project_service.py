"""Project service (contract §6)."""
from __future__ import annotations

from app.core.errors import conflict, not_found
from app.core.security import Principal
from app.services.audit_service import AuditService
from app.services.ids import new_id
from app.services.events import EventBus


def _slug(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


class ProjectService:
    def __init__(self, db, audit: AuditService, events: EventBus) -> None:
        self._db = db
        self._audit = audit
        self._events = events

    async def list(self, page: int, size: int) -> tuple[list[dict], int]:
        return await self._db.projects.list(page, size)

    async def create(self, principal: Principal, name: str, priority: str, description: str | None = None,
                     version: str = "0.1", repository_url: str | None = None, deployment_url: str | None = None) -> dict:
        existing = await self._db.fetchone("SELECT id FROM projects WHERE slug = %s", _slug(name))
        if existing:
            raise conflict(f"Project with name '{name}' already exists", {"name": name})
        project = await self._db.projects.create(new_id("prj"), name, _slug(name), priority, description, version,
                                                 repository_url, deployment_url)
        await self._db.projects.touch_activity(project["id"])
        await self._audit.log("PROJECT_CREATED", principal, "SUCCESS", "project", project["id"], project_id=project["id"])
        await self._events.publish("project.updated", "api", project["id"], "INFO", {"change": "created"})
        return project

    async def get_detail(self, project_id: str) -> dict:
        project = await self._db.projects.detail(project_id)
        if not project:
            raise not_found("project")
        return project

    async def update(self, principal: Principal, project_id: str, fields: dict) -> dict:
        project = await self._db.projects.get(project_id)
        if not project:
            raise not_found("project")
        updated = await self._db.projects.update(project_id, fields)
        if updated is None:
            raise conflict("Project state conflict")
        await self._db.projects.touch_activity(project_id)
        await self._audit.log("PROJECT_UPDATED", principal, "SUCCESS", "project", project_id,
                              project_id=project_id, details={"fields": list(fields.keys())})
        await self._events.publish("project.updated", "api", project_id, "INFO", {"change": "updated"})
        return await self._db.projects.detail(project_id)
