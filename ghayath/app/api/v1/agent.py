"""Agent API (contract §§4-5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from app.core.envelope import build_success
from app.core.errors import validation_error
from app.core.registry import register
from app.core.security import Principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Agent"])


def _s(x):
    """Generated ID fields are pydantic RootModel[str]; unwrap for services/SQL."""
    return x.root if x is not None and hasattr(x, "root") else x


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    return idempotency_key


@router.post("/agent/command", operation_id="agentCommand", response_model=models.AgentCommandResponse, status_code=201)
async def agent_command(request: Request, body: models.AgentCommandRequest,
                        principal: Principal = Depends(require_mutating)):
    services = request.app.state.services
    data = await services["agent"].command(
        principal, body.command, body.source or "dashboard",
        body.conversation_id.root if body.conversation_id else None,
        bool(body.autonomous),
    )
    return build_success(models.AgentCommandResponse, data)


@router.post("/agent/execute", operation_id="agentExecute", response_model=models.AgentExecuteResponse, status_code=202)
async def agent_execute(request: Request, body: models.AgentExecuteRequest,
                        principal: Principal = Depends(require_mutating),
                        idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    services = request.app.state.services
    idem = services["idempotency"]
    approval_id = _s(body.approval_id)
    stored = await idem.check("agent.execute", key, {"command_id": body.command_id, "approval_id": approval_id})
    if stored is not None:
        return stored
    data = await services["agent"].execute(principal, body.command_id, approval_id)
    response = build_success(models.AgentExecuteResponse, data)
    await idem.record("agent.execute", key, {"command_id": body.command_id, "approval_id": approval_id}, 202, response)
    return response

# Security registry (contract-drift test reads this at import time).
register("POST", "/agent/command", "AUTH")
register("POST", "/agent/execute", "AUTH")
