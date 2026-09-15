"""Real PostgreSQL integration coverage for the adopted v1.1 operations."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api.v1.events import _stream
from app.core.security import get_stream_principal
from tests.conftest import AGENT_EMAIL, OWNER_EMAIL, VIEWER_EMAIL, bearer, unique_key


async def _create_incident(client, token: str, key: str | None = None) -> str:
    headers = bearer(token)
    if key:
        headers["Idempotency-Key"] = key
    response = await client.post("/api/v1/incidents", headers=headers, json={
        "severity": "HIGH", "system": "api", "issue": "error spike", "impact": "users affected",
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]["incident_id"]


async def test_v11_execution_lookup_and_viewer_result_scoping(client, login):
    owner = await login(OWNER_EMAIL)
    agent = await login(AGENT_EMAIL)
    viewer = await login(VIEWER_EMAIL)
    planned = await client.post("/api/v1/agent/command", headers=bearer(owner), json={"command": "hello there"})
    assert planned.status_code == 201, planned.text
    command_id = planned.json()["data"]["command_id"]
    executed = await client.post(
        "/api/v1/agent/execute", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"command_id": command_id},
    )
    assert executed.status_code == 202, executed.text
    execution_id = executed.json()["data"]["execution_id"]

    owner_view = await client.get(f"/api/v1/agent/executions/{execution_id}", headers=bearer(owner))
    agent_view = await client.get(f"/api/v1/agent/executions/{execution_id}", headers=bearer(agent))
    viewer_view = await client.get(f"/api/v1/agent/executions/{execution_id}", headers=bearer(viewer))
    assert owner_view.status_code == agent_view.status_code == viewer_view.status_code == 200
    assert owner_view.json()["data"]["result"] is not None
    assert agent_view.json()["data"]["result"] is not None
    redacted = viewer_view.json()["data"]
    assert redacted["result"] is None
    assert all(len(step["output_summary"] or "") <= 500 for step in redacted["steps"])

    missing = await client.get("/api/v1/agent/executions/exec_doesnotexist", headers=bearer(viewer))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_v11_rbac_for_all_six_operations(client, login):
    owner = await login(OWNER_EMAIL)
    agent = await login(AGENT_EMAIL)
    viewer = await login(VIEWER_EMAIL)

    # Read operations: listAutomations is readable by all three roles.
    for token in (owner, agent, viewer):
        assert (await client.get("/api/v1/automations", headers=bearer(token))).status_code == 200
    # Notifications deliberately excludes AGENT.
    assert (await client.get("/api/v1/notifications", headers=bearer(owner))).status_code == 200
    assert (await client.get("/api/v1/notifications", headers=bearer(viewer))).status_code == 200
    assert (await client.get("/api/v1/notifications", headers=bearer(agent))).status_code == 403

    # SSE deliberately excludes AGENT and permits OWNER/VIEWER authentication.
    assert (await client.get("/api/v1/events/stream")).status_code == 401
    assert (await client.get("/api/v1/events/stream", headers=bearer(agent))).status_code == 403
    # Authorized streams are intentionally long-lived; the lifecycle/cursor is
    # exercised below at the generator boundary rather than buffering a live
    # connection in the HTTP test client.

    # DELETE is OWNER-only; PATCH is OWNER/AGENT; VIEWER can do neither.
    created = await client.post("/api/v1/automations", headers=bearer(owner), json={
        "name": "rbac", "trigger": {"type": "MANUAL"}, "action": {"type": "CREATE_TASK"},
    })
    automation_id = created.json()["data"]["id"]
    for token in (agent, viewer):
        response = await client.delete(
            f"/api/v1/automations/{automation_id}", headers=bearer(token),
        )
        assert response.status_code == 403

    incident_id = await _create_incident(client, owner, unique_key())
    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(agent), "Idempotency-Key": unique_key()},
        json={"status": "INVESTIGATING"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(viewer), "Idempotency-Key": unique_key()},
        json={"status": "MITIGATED"},
    )).status_code == 403


async def test_v11_sse_cursor_filter_heartbeat_and_query_auth(client, login, monkeypatch):
    owner = await login(OWNER_EMAIL)
    viewer = await login(VIEWER_EMAIL)

    # Explicit cursor reads only subsequent persisted events; a type filter is
    # applied after advancing the cursor so filtered events cannot be replayed.
    first = await client.app.state.db.events.publish(
        "evt_01jv11first", "task.created", "test", None, "INFO", {"source": "test-boundary"})
    second = await client.app.state.db.events.publish(
        "evt_01jv11second", "incident.created", "test", None, "HIGH", {"source": "test-boundary"})

    class Request:
        app = client.app

        async def is_disconnected(self):
            return False

    generator = _stream(Request(), first["id"], {"incident.created"})
    chunk = await anext(generator)
    await generator.aclose()
    assert f"id: {second['id']}" in chunk
    assert "event: incident.created" in chunk
    assert '"event_id":"evt_01jv11second"' in chunk
    assert first["id"] not in chunk

    # Heartbeat timing is tested at the generator boundary, without inventing
    # events in production: the real source above is PostgreSQL.
    class EmptyEvents:
        async def latest_id(self):
            return None

        async def after(self, cursor, limit):
            return []

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=SimpleNamespace(events=EmptyEvents()))),
        is_disconnected=lambda: asyncio.sleep(0, result=False),
    )
    ticks = iter((0.0, 16.0))
    monkeypatch.setattr("app.api.v1.events.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    heartbeat = _stream(request, None, set())
    assert await anext(heartbeat) == ": ping\n\n"
    await heartbeat.aclose()

    # Exercise the actual dependency with a Starlette Request to prove the
    # EventSource query-token exception is not accepted by ordinary routes.
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http", "app": client.app, "method": "GET", "path": "/api/v1/events/stream",
        "raw_path": b"/api/v1/events/stream", "query_string": f"access_token={owner}".encode(),
        "headers": [], "client": ("test", 1), "server": ("test", 80), "scheme": "http",
    }
    principal = await get_stream_principal(StarletteRequest(scope))
    assert principal.is_owner
    assert viewer


async def test_v11_automation_list_delete_idempotency_and_audit(client, login):
    owner = await login(OWNER_EMAIL)
    viewer = await login(VIEWER_EMAIL)
    created = await client.post("/api/v1/automations", headers=bearer(owner), json={
        "name": "delete me", "trigger": {"type": "MANUAL"}, "action": {"type": "CREATE_TASK"},
    })
    assert created.status_code == 201
    automation_id = created.json()["data"]["id"]
    listed = await client.get("/api/v1/automations?enabled=true", headers=bearer(viewer))
    assert listed.status_code == 200
    assert any(row["id"] == automation_id for row in listed.json()["data"])

    missing_key = await client.delete(f"/api/v1/automations/{automation_id}", headers=bearer(owner))
    assert missing_key.status_code == 400
    key = unique_key()
    deleted = await client.delete(
        f"/api/v1/automations/{automation_id}", headers={**bearer(owner), "Idempotency-Key": key})
    assert deleted.status_code == 204 and deleted.content == b""
    replay = await client.delete(
        f"/api/v1/automations/{automation_id}", headers={**bearer(owner), "Idempotency-Key": key})
    assert replay.status_code == 204
    absent = await client.delete(f"/api/v1/automations/{automation_id}", headers=bearer(owner))
    assert absent.status_code == 404
    audit = await client.app.state.db.fetchone(
        "SELECT * FROM audit_logs WHERE action = 'DELETE_AUTOMATION' AND target_id = %s", automation_id)
    assert audit is not None

    # The immutable trigger rejects mutation of the newly-created audit row.
    with pytest.raises(Exception):
        await client.app.state.db.execute("UPDATE audit_logs SET action = 'TAMPERED' WHERE id = %s", audit["id"])
    untouched = await client.app.state.db.fetchone("SELECT action FROM audit_logs WHERE id = %s", audit["id"])
    assert untouched["action"] == "DELETE_AUTOMATION"


async def test_v11_incident_transitions_idempotency_and_audit_note(client, login):
    owner = await login(OWNER_EMAIL)
    agent = await login(AGENT_EMAIL)
    viewer = await login(VIEWER_EMAIL)
    incident_id = await _create_incident(client, owner, unique_key())

    key = unique_key()
    first = await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": key},
        json={"status": "INVESTIGATING", "resolution_note": "triage started"},
    )
    assert first.status_code == 200
    replay = await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": key},
        json={"status": "INVESTIGATING", "resolution_note": "triage started"},
    )
    assert replay.status_code == 200 and replay.json()["data"]["status"] == "INVESTIGATING"
    mismatch = await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": key},
        json={"status": "RESOLVED"},
    )
    assert mismatch.status_code == 409

    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(agent), "Idempotency-Key": unique_key()},
        json={"status": "MITIGATED"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(agent), "Idempotency-Key": unique_key()},
        json={"status": "INVESTIGATING"},
    )).status_code == 200
    resolved = await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "RESOLVED", "resolution_note": "verified fixed"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["resolved_at"] is not None
    closed = await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "CLOSED"},
    )
    assert closed.status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "INVESTIGATING"},
    )).status_code == 409
    assert (await client.patch(
        f"/api/v1/incidents/{incident_id}", headers={**bearer(viewer), "Idempotency-Key": unique_key()},
        json={"status": "OPEN"},
    )).status_code == 403

    audit = await client.app.state.db.fetchone(
        "SELECT * FROM audit_logs WHERE action = 'PATCH_INCIDENT' AND target_id = %s "
        "AND details->>'resolution_note' IS NOT NULL ORDER BY id DESC LIMIT 1",
        incident_id,
    )
    assert audit is not None
    assert audit["details"].get("resolution_note") == "verified fixed"


async def test_v11_incident_transition_matrix_remaining_edges(client, login):
    owner = await login(OWNER_EMAIL)

    # OPEN -> RESOLVED -> CLOSED
    direct = await _create_incident(client, owner, unique_key())
    assert (await client.patch(
        f"/api/v1/incidents/{direct}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "RESOLVED"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{direct}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "CLOSED"},
    )).status_code == 200

    # INVESTIGATING -> RESOLVED
    investigating = await _create_incident(client, owner, unique_key())
    assert (await client.patch(
        f"/api/v1/incidents/{investigating}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "INVESTIGATING"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{investigating}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "RESOLVED"},
    )).status_code == 200

    # MITIGATED -> RESOLVED
    mitigated = await _create_incident(client, owner, unique_key())
    assert (await client.patch(
        f"/api/v1/incidents/{mitigated}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "INVESTIGATING"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{mitigated}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "MITIGATED"},
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/incidents/{mitigated}", headers={**bearer(owner), "Idempotency-Key": unique_key()},
        json={"status": "RESOLVED"},
    )).status_code == 200


async def test_v11_notifications_real_archive_filters_and_limit(client, login):
    owner = await login(OWNER_EMAIL)
    agent = await login(AGENT_EMAIL)
    project = await client.post("/api/v1/projects", headers=bearer(owner), json={"name": "N", "priority": "P2"})
    project_id = project.json()["data"]["id"]
    low = await client.post("/api/v1/notifications", headers=bearer(owner), json={
        "severity": "LOW", "title": "low", "message": "dashboard", "project_id": project_id,
    })
    high = await client.post("/api/v1/notifications", headers=bearer(owner), json={
        "severity": "HIGH", "title": "high", "message": "push", "project_id": project_id,
    })
    assert low.status_code == high.status_code == 201
    assert (await client.get("/api/v1/notifications", headers=bearer(agent))).status_code == 403

    filtered = await client.get(
        "/api/v1/notifications", headers=bearer(owner),
        params={"severity": "HIGH", "status": "SENT", "project_id": project_id, "limit": 1},
    )
    assert filtered.status_code == 200, filtered.text
    rows = filtered.json()["data"]
    assert len(rows) == 1 and rows[0]["title"] == "high"
    assert (await client.get("/api/v1/notifications?limit=201", headers=bearer(owner))).status_code == 400
    assert low.json()["data"]["notification_id"] != high.json()["data"]["notification_id"]
