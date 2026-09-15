"""Automations API (v1 + additive v1.1 list/delete operations)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, Request
from starlette.responses import Response

from app.core.envelope import build_success
from app.core.errors import not_found, validation_error
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating, require_owner
from app.generated import models

router = APIRouter(tags=["Automations"])


def _require_key(value: str | None) -> str:
    if value is None or not 8 <= len(value) <= 128 or re.fullmatch(r"[A-Za-z0-9._-]{8,128}", value) is None:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory and must match the contract format"})
    return value


@router.get("/automations", operation_id="listAutomations", response_model=models.EnvelopeAutomationList)
async def list_automations(request: Request, enabled: bool | None = None,
                           principal: Principal = Depends(get_principal)):
    del principal
    rows = await request.app.state.services["automations"].list(enabled)
    return build_success(models.EnvelopeAutomationList, rows)


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


@router.delete("/automations/{automation_id}", operation_id="deleteAutomation", status_code=204)
async def delete_automation(request: Request, automation_id: str,
                            principal: Principal = Depends(require_owner),
                            idempotency_key: str | None = Header(default=None)):
    services = request.app.state.services
    # The adopted contract explicitly keeps DELETE's usual 404 behavior when
    # the resource is already gone and no key was supplied.  A valid replay key
    # is checked first below so a prior 204 still replays after hard deletion.
    if idempotency_key is None and await services["automations"].get(automation_id) is None:
        raise not_found("automation")
    key = _require_key(idempotency_key)
    idem = services["idempotency"]
    body = {"automation_id": automation_id}
    stored = await idem.check("automation.delete", key, body)
    if stored is not None:
        return stored
    await services["automations"].delete(principal, automation_id)
    await idem.record("automation.delete", key, body, 204, {})
    return Response(status_code=204)


# Security registry (contract-drift test reads this at import time).
register("GET", "/automations", "AUTH")
register("POST", "/automations", "AUTH")
register("PATCH", "/automations/{automation_id}", "AUTH")
register("DELETE", "/automations/{automation_id}", "AUTH")
