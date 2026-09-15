"""Automations API (contract §16). NOTE: list/delete are deferred to v1.1 by contract."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Automations"])


@router.post("/automations", operation_id="createAutomation", response_model=models.AutomationResponse, status_code=201)
async def create_automation(request: Request, body: models.CreateAutomationRequest,
                            principal: Principal = Depends(require_mutating)):
    row = await request.app.state.services["automations"].create(
        principal, body.name, body.trigger.model_dump(mode="json"), body.action.model_dump(mode="json"),
        body.enabled if body.enabled is not None else True, body.description)
    return build_success(models.AutomationResponse, row)


@router.patch("/automations/{automation_id}", operation_id="updateAutomation", response_model=models.AutomationResponse)
async def update_automation(request: Request, automation_id: str, body: models.UpdateAutomationRequest,
                            principal: Principal = Depends(require_mutating)):
    dump = body.model_dump(exclude_unset=True, mode="json")
    row = await request.app.state.services["automations"].update(
        principal, automation_id,
        name=dump.get("name"), description=dump.get("description"),
        trigger=dump.get("trigger"), action=dump.get("action"), enabled=dump.get("enabled"),
    )
    return build_success(models.AutomationResponse, row)

# Security registry (contract-drift test reads this at import time).
register("POST", "/automations", "AUTH")
register("PATCH", "/automations/{automation_id}", "AUTH")
