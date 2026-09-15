"""Incidents API (v1 create + v1.1 state transitions)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, Request

from app.core.envelope import build_success
from app.core.errors import validation_error
from app.core.registry import register
from app.core.security import Principal, require_mutating
from app.generated import models

router = APIRouter(tags=["Incidents"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128 or re.fullmatch(r"[A-Za-z0-9._-]{8,128}", idempotency_key) is None:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory and must match the contract format"})
    return idempotency_key


@router.post("/incidents", operation_id="createIncident", response_model=models.IncidentResponse, status_code=201)
async def create_incident(request: Request, body: models.CreateIncidentRequest,
                          principal: Principal = Depends(require_mutating),
                          idempotency_key: str | None = Header(default=None)):
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    services = request.app.state.services
    idem = services["idempotency"]
    idem_body = {
        "severity": _v(body.severity), "system": body.system,
        "issue": body.issue, "impact": body.impact,
        "source": _v(body.source) if body.source else "agent",
    }
    stored = await idem.check("incident.create", idempotency_key, idem_body)
    if stored is not None:
        return stored
    incident = await services["incidents"].create(
        principal, _v(body.severity), body.system, body.issue, body.impact,
        _v(body.source) if body.source else "agent")
    response = build_success(models.IncidentResponse, {
        "incident_id": incident["id"], "status": "OPEN", "severity": incident["severity"],
        "system": incident["system"],
        "created_at": incident["created_at"].isoformat().replace("+00:00", "Z"),
    })
    await idem.record("incident.create", idempotency_key, idem_body, 201, response)
    return response


@router.patch("/incidents/{incident_id}", operation_id="patchIncident", response_model=models.EnvelopeIncident)
async def patch_incident(request: Request, incident_id: str, body: models.IncidentStatusUpdateRequest,
                         principal: Principal = Depends(require_mutating),
                         idempotency_key: str | None = Header(default=None)):
    key = _require_idempotency_key(idempotency_key)
    services = request.app.state.services
    idem = services["idempotency"]
    idem_body = {"incident_id": incident_id, **body.model_dump(mode="json")}
    stored = await idem.check("incident.patch", key, idem_body)
    if stored is not None:
        return stored
    incident = await services["incidents"].transition(
        principal, incident_id, _v(body.status), body.resolution_note)
    # The generated projection carries the existing v1 source enum. Pass the
    # enum value explicitly so Pydantic does not serialize the contract's
    # string default as an unexpected enum value.
    incident_view = dict(incident)
    if incident_view.get("source") is not None:
        incident_view["source"] = models.Source1(incident_view["source"])
    response = build_success(models.EnvelopeIncident, incident_view)
    await idem.record("incident.patch", key, idem_body, 200, response)
    return response


# Security registry (contract-drift test reads this at import time).
register("POST", "/incidents", "AUTH")
register("PATCH", "/incidents/{incident_id}", "AUTH")
