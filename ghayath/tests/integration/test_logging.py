"""Validation 19: structured logging with request/actor context; sensitive values
(passwords, tokens, secrets, authorization headers) never appear in logs."""
from __future__ import annotations

import json
import logging

import pytest

from tests.conftest import OWNER_EMAIL, PASSWORD, WEBHOOK_SECRET

SECRET_JWT = "test-jwt-secret-not-for-production"


class _ListHandler(logging.Handler):
    """Captures the FORMATTED line at emit time (request context is live then)."""

    def __init__(self):
        from app.core.context import StructuredFormatter

        super().__init__(level=logging.DEBUG)
        self.records = []
        self._fmt = StructuredFormatter()

    def emit(self, record: logging.LogRecord):
        try:
            self.records.append(self._fmt.format(record))
        except Exception:
            self.records.append(record.getMessage())


async def test_requests_are_logged_with_context_and_no_secrets(client):
    handler = _ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD})
        assert r.status_code == 200
        tok = r.json()["data"]["access_token"]
        rid = r.headers.get("X-Request-Id")
        r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}", "X-Request-Id": rid})
        assert r.status_code == 200
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    assert handler.records, "no log records captured"
    blob = "\n".join(handler.records)
    # request context was emitted
    assert rid in blob, "request_id must appear in structured logs"
    # no secrets, ever
    assert PASSWORD not in blob
    assert SECRET_JWT not in blob
    assert WEBHOOK_SECRET not in blob
    assert tok not in blob
    assert "Bearer" not in blob


async def test_redaction_function_blocks_sensitive_keys():
    from app.core.context import redact

    out = redact({
        "password": "hunter2",
        "api_key": "abc",
        "authorization": "Bearer xyz",
        "webhook_signature": "sig",
        "refresh_token": "rt",
        "nested": {"client_secret": "s", "safe": 1},
        "list": [{"private_key": "k"}],
    })
    assert out["password"] == "***REDACTED***"
    assert out["api_key"] == "***REDACTED***"
    assert out["authorization"] == "***REDACTED***"
    assert out["webhook_signature"] == "***REDACTED***"
    assert out["refresh_token"] == "***REDACTED***"
    assert out["nested"]["client_secret"] == "***REDACTED***"
    assert out["nested"]["safe"] == 1
    assert out["list"][0]["private_key"] == "***REDACTED***"
