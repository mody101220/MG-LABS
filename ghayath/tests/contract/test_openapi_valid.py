"""Validation 1-3: OpenAPI 3.1 validity, $ref resolution, unique operationIds,
fixed error-code set, documented HTTP mapping."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

SPEC = Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml"


def _spec() -> dict:
    return yaml.safe_load(SPEC.read_text())


def test_openapi_31_valid():
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    spec, _ = read_from_filename(str(SPEC))
    validate(spec)  # raises on any schema violation


def test_all_component_refs_resolve():
    spec = _spec()
    known = set()
    for section in ("schemas", "responses", "parameters", "headers"):
        known |= {f"components/{section}/{k}" for k in spec.get("components", {}).get(section, {})}
    raw = SPEC.read_text()
    refs = set(re.findall(r"#/(components/(?:schemas|responses|parameters|headers)/[\w-]+)", raw))
    refs = {r.lstrip("#/") for r in refs}
    unresolved = {r for r in refs if r not in known}
    assert not unresolved, f"unresolved refs: {unresolved}"


def test_operation_ids_unique_and_counted():
    spec = _spec()
    ids = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                assert "operationId" in op, f"{method} {path} missing operationId"
                ids.append(op["operationId"])
    assert len(ids) == len(set(ids)), "duplicate operationIds"
    assert len(ids) == 54, f"expected 54 operations, found {len(ids)}"


def test_error_codes_are_exactly_the_fixed_set():
    spec = _spec()
    codes = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
    expected = {
        "AUTHENTICATION_FAILED", "INVALID_TOKEN", "PERMISSION_DENIED", "APPROVAL_REQUIRED",
        "VALIDATION_ERROR", "RESOURCE_NOT_FOUND", "CONFLICT", "RATE_LIMITED",
        "INTEGRATION_OFFLINE", "TOOL_UNAVAILABLE", "EXECUTION_FAILED", "VERIFICATION_FAILED",
        "TIMEOUT", "DEPENDENCY_FAILURE", "SECRET_ACCESS_DENIED",
    }
    assert codes == expected


def test_v11_operations_are_adopted():
    spec = _spec()
    paths = set(spec["paths"])
    assert "/agent/executions/{execution_id}" in paths
    assert "/events/stream" in paths
    assert "get" in spec["paths"]["/automations"]
    assert "delete" in spec["paths"]["/automations/{automation_id}"]
    assert "patch" in spec["paths"]["/incidents/{incident_id}"]
    assert "get" in spec["paths"]["/notifications"]
