"""Agent execution lookup (v1.1), read-only and DB-backed."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal
from app.generated import models

router = APIRouter(tags=["Agent"])


@router.get("/agent/executions/{execution_id}", operation_id="getExecution", response_model=models.EnvelopeAgentExecution)
async def get_execution(request: Request, execution_id: str,
                        principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["executions"].get(principal, execution_id)
    return build_success(models.EnvelopeAgentExecution, data)


# Security registry (contract-drift test reads this at import time).
register("GET", "/agent/executions/{execution_id}", "AUTH")
