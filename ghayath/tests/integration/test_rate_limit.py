"""Validation 10: rate limiting exactly per contract §28 (auth 5/min, agent 60/min,
documented 429 + X-RateLimit-Reset; no undocumented limits)."""
from __future__ import annotations

import pytest

from tests.conftest import AGENT_EMAIL, OWNER_EMAIL, bearer


async def test_agent_command_bucket_60_per_minute(client, login):
    t = await login(AGENT_EMAIL)
    codes = []
    for i in range(61):
        r = await client.post("/api/v1/agent/command", headers=bearer(t),
                              json={"command": f"hello {i}"})
        codes.append(r.status_code)
    assert codes[:60] == [201] * 60, f"first 60 must pass, got first failure at {codes.index(429)}"
    assert codes[60] == 429
    last = await client.post("/api/v1/agent/command", headers=bearer(t), json={"command": "one more"})
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "RATE_LIMITED"
    assert "X-RateLimit-Reset" in last.headers
    assert int(last.headers["X-RateLimit-Reset"]) > 0


async def test_unlisted_endpoints_are_not_limited(client, login):
    """Contract only limits /auth/* and /agent/{command,execute}; everything else is unlimited."""
    t = await login(OWNER_EMAIL)
    for i in range(8):
        r = await client.post("/api/v1/projects", headers=bearer(t),
                              json={"name": f"unlimited {i}", "priority": "P3"})
        assert r.status_code == 201, f"project {i} unexpectedly limited"


async def test_auth_bucket_per_scope(client):
    """6th auth attempt from the same scope is 429 (bucket = client for anonymous)."""
    for i in range(5):
        r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "wrong-password-1"})
        assert r.status_code == 401, f"attempt {i} should be 401"
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "wrong-password-1"})
    assert r.status_code == 429
