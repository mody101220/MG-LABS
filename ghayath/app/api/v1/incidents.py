"""Incidents API (contract §22). NOTE: status mutation is deferred to v1.1 by contract."""
from __future__ import annotations

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


@router.post("/incidents", operation_id="createIncident", response_model=models.IncidentResponse, status_code=201)
async def create_incident(request: Request, body: models.CreateIncidentRequest,
                          principal: Principal = Depends(require_mutating),
                          idempotency_key: str | None = Header(default=None)):
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise validation_error({"field": "Idempotency-Key", "reason": "header is mandatory for this operation"})
    services = request.app.state.services
    idem = services["idempotency"]
    stored = await idem.check("incident.create", idempotency_key, body.model_dump(mode="json"))
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
    await idem.record("incident.create", idempotency_key, body.model_dump(mode="json"), 201, response)
    return response

# Security registry (contract-drift test reads this at import time).
register("POST", "/incidents", "AUTH")
