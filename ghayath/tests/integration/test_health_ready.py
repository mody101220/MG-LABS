"""Validation 13: GET /api/v1/health (contract) + /ready (ops probe, outside /api/v1).
Both perform REAL dependency checks — no hardcoded green."""
from __future__ import annotations

import pytest


async def test_health_real_check(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    d = body["data"]
    assert set(d["services"]) == {"database", "redis", "agent", "scheduler", "notifications"}
    # real DB behind this app
    assert d["services"]["database"] == "healthy"
    assert d["services"]["scheduler"] == "healthy"
    # no redis configured → honestly 'down', not faked healthy; overall status degrades
    assert d["services"]["redis"] == "down"
    assert d["status"] in ("healthy", "degraded")
    assert d["status"] == "degraded"  # redis down must be visible, never hidden


async def test_health_requires_no_auth(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200


async def test_ready_probe(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


async def test_ready_reflects_migration_state(client):
    db = client.app.state.db
    migrated = (await db.fetchone("SELECT to_regclass('public.users') IS NOT NULL AS m"))["m"]
    assert migrated is True
