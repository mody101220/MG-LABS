"""Memory API (contract §8)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Memory"])


def _s(x):
    """Generated ID fields are pydantic RootModel[str]; unwrap for services/SQL."""
    return x.root if x is not None and hasattr(x, "root") else x


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.post("/memory", operation_id="createMemory", response_model=models.MemoryResponse, status_code=201)
async def create_memory(request: Request, body: models.CreateMemoryRequest,
                        principal: Principal = Depends(require_mutating)):
    row = await request.app.state.services["memory"].write(
        principal, _v(body.type), body.key, body.value,
        _s(body.project_id), _v(body.source) if body.source else "agent",
        None, body.expires_at,
    )
    return build_success(models.MemoryResponse, row)


@router.get("/memory", operation_id="listMemory", response_model=models.MemoryListResponse)
async def list_memory(request: Request,
                      type: models.MemoryType | None = Query(default=None),
                      project_id: str | None = Query(default=None),
                      key: str | None = Query(default=None),
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                      principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["memory"].list(
        type.value if type else None, project_id, key, page, page_size)
    return build_success(models.MemoryListResponse, rows)


@router.delete("/memory/{memory_id}", operation_id="deleteMemory", response_model=models.MemoryDeleteResponse)
async def delete_memory(request: Request, memory_id: str, principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["memory"].delete(principal, memory_id)
    return build_success(models.MemoryDeleteResponse, data)

# Security registry (contract-drift test reads this at import time).
register("POST", "/memory", "AUTH")
register("GET", "/memory", "AUTH")
register("DELETE", "/memory/{memory_id}", "AUTH")
