"""Tasks API (contract §7) — completion is verification-gated."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.envelope import build_success
from app.core.errors import validation_error
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Tasks"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    return idempotency_key


@router.post("/tasks", operation_id="createTask", response_model=models.TaskResponse, status_code=201)
async def create_task(request: Request, body: models.CreateTaskRequest,
                      principal: Principal = Depends(require_mutating)):
    task = await request.app.state.services["tasks"].create(
        principal, body.project_id, body.title, _v(body.priority),
        _v(body.status) if body.status else "TODO", body.description,
        body.assignee, body.due_at,
    )
    return build_success(models.TaskResponse, task)


@router.get("/tasks", operation_id="listTasks", response_model=models.TaskListResponse)
async def list_tasks(request: Request,
                     project_id: str | None = Query(default=None),
                     status: models.TaskStatus | None = Query(default=None),
                     priority: models.Priority | None = Query(default=None),
                     assignee: str | None = Query(default=None),
                     due_before: datetime | None = Query(default=None),
                     page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["tasks"].list(
        project_id, status.value if status else None, priority.value if priority else None,
        assignee, due_before, page, page_size)
    return build_success(models.TaskListResponse, rows)


@router.patch("/tasks/{task_id}", operation_id="updateTask", response_model=models.TaskResponse)
async def update_task(request: Request, task_id: str, body: models.UpdateTaskRequest,
                      principal: Principal = Depends(require_mutating)):
    fields = body.model_dump(exclude_unset=True, mode="json")
    task = await request.app.state.services["tasks"].update(principal, task_id, fields)
    return build_success(models.TaskResponse, task)


@router.post("/tasks/{task_id}/complete", operation_id="completeTask", response_model=models.TaskCompleteResponse)
async def complete_task(request: Request, task_id: str, body: models.TaskCompleteRequest,
                        principal: Principal = Depends(require_mutating),
                        idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    services = request.app.state.services
    idem = services["idempotency"]
    stored = await idem.check("task.complete", key, {"task_id": task_id, "verification": body.verification.model_dump()})
    if stored is not None:
        return stored
    data = await services["tasks"].complete(principal, task_id, bool(body.verification.required))
    response = build_success(models.TaskCompleteResponse, data)
    await idem.record("task.complete", key, {"task_id": task_id, "verification": body.verification.model_dump()}, 200, response)
    return response

# Security registry (contract-drift test reads this at import time).
register("POST", "/tasks", "AUTH")
register("GET", "/tasks", "AUTH")
register("PATCH", "/tasks/{task_id}", "AUTH")
register("POST", "/tasks/{task_id}/complete", "AUTH")
