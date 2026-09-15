"""Projects API (contract §6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Projects"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.get("/projects", operation_id="listProjects", response_model=models.ProjectListResponse)
async def list_projects(request: Request,
                        page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                        principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["projects"].list(page, page_size)
    return build_success(models.ProjectListResponse, rows)


@router.post("/projects", operation_id="createProject", response_model=models.ProjectResponse, status_code=201)
async def create_project(request: Request, body: models.CreateProjectRequest,
                         principal: Principal = Depends(require_mutating)):
    project = await request.app.state.services["projects"].create(
        principal, body.name, _v(body.priority), body.description,
        body.version or "0.1", str(body.repository_url) if body.repository_url else None,
        str(body.deployment_url) if body.deployment_url else None,
    )
    return build_success(models.ProjectResponse, project)


@router.get("/projects/{project_id}", operation_id="getProject", response_model=models.ProjectResponse)
async def get_project(request: Request, project_id: str, principal: Principal = Depends(get_principal)):
    detail = await request.app.state.services["projects"].get_detail(project_id)
    return build_success(models.ProjectResponse, detail)


@router.patch("/projects/{project_id}", operation_id="updateProject", response_model=models.ProjectResponse)
async def update_project(request: Request, project_id: str, body: models.UpdateProjectRequest,
                         principal: Principal = Depends(require_mutating)):
    fields = {k: (v.value if isinstance(v, models.BaseModel) and hasattr(v, "value") else str(v) if isinstance(v, (models.AnyUrl,)) else v)
              for k, v in body.model_dump(exclude_unset=True, mode="json").items()}
    detail = await request.app.state.services["projects"].update(principal, project_id, fields)
    return build_success(models.ProjectResponse, detail)

# Security registry (contract-drift test reads this at import time).
register("GET", "/projects", "AUTH")
register("POST", "/projects", "AUTH")
register("GET", "/projects/{project_id}", "AUTH")
register("PATCH", "/projects/{project_id}", "AUTH")
