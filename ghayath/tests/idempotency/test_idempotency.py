"""Validation 11: Idempotency-Key — mandatory where contracted, same key + same body
replays the original result with no duplicate side effect, same key + different body
is a CONFLICT."""
from __future__ import annotations


from tests.conftest import OWNER_EMAIL, bearer, unique_key

from tests.integration.test_api_contract import MockProviders, connect_mock


def _idem_headers(t: str, key: str) -> dict:
    return {**bearer(t), "Idempotency-Key": key}


async def test_whatsapp_send_replay_no_duplicate(client, login):
    t = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    key = unique_key()
    body = {"recipient": "+963930123456", "message": "hello there"}
    r1 = await client.post("/api/v1/whatsapp/messages/send", json=body, headers=_idem_headers(t, key))
    assert r1.status_code == 200, r1.text
    first = r1.json()["data"]
    r2 = await client.post("/api/v1/whatsapp/messages/send", json=body, headers=_idem_headers(t, key))
    assert r2.status_code == 200
    assert r2.json()["data"]["message_id"] == first["message_id"]
    assert len(providers.requests) == 1, "provider must be called exactly once"
    row = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM whatsapp_messages WHERE idempotency_key = %s", key)
    assert row["n"] == 1


async def test_whatsapp_send_same_key_different_body_conflict(client, login):
    t = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    key = unique_key()
    r1 = await client.post("/api/v1/whatsapp/messages/send",
                           json={"recipient": "+963930123456", "message": "first"},
                           headers=_idem_headers(t, key))
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/whatsapp/messages/send",
                           json={"recipient": "+963930123456", "message": "different"},
                           headers=_idem_headers(t, key))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "CONFLICT"
    assert len(providers.requests) == 1


async def test_agent_execute_replay_no_duplicate_execution(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t), json={"command": "hello there"})
    cmd = r.json()["data"]["command_id"]
    key = unique_key()
    r1 = await client.post("/api/v1/agent/execute", json={"command_id": cmd}, headers=_idem_headers(t, key))
    assert r1.status_code == 202
    first = r1.json()["data"]["execution_id"]
    r2 = await client.post("/api/v1/agent/execute", json={"command_id": cmd}, headers=_idem_headers(t, key))
    assert r2.status_code == 202
    assert r2.json()["data"]["execution_id"] == first
    count = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM executions WHERE command_id = %s", cmd)
    assert count["n"] == 1


async def test_agent_execute_same_key_different_command_conflict(client, login):
    t = await login(OWNER_EMAIL)
    c1 = (await client.post("/api/v1/agent/command", headers=bearer(t),
                            json={"command": "hello there"})).json()["data"]["command_id"]
    c2 = (await client.post("/api/v1/agent/command", headers=bearer(t),
                            json={"command": "hello again"})).json()["data"]["command_id"]
    key = unique_key()
    r1 = await client.post("/api/v1/agent/execute", json={"command_id": c1}, headers=_idem_headers(t, key))
    assert r1.status_code == 202
    r2 = await client.post("/api/v1/agent/execute", json={"command_id": c2}, headers=_idem_headers(t, key))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "CONFLICT"


async def test_task_complete_replay_and_conflict(client, login):
    t = await login(OWNER_EMAIL)
    pr = await client.post("/api/v1/projects", headers=bearer(t), json={"name": "P", "priority": "P1"})
    pid = pr.json()["data"]["id"]
    tid = (await client.post("/api/v1/tasks", headers=bearer(t),
                             json={"project_id": pid, "title": "T", "priority": "P1"})).json()["data"]["id"]
    key = unique_key()
    body = {"verification": {"required": False, "status": "NOT_REQUIRED"}}
    r1 = await client.post(f"/api/v1/tasks/{tid}/complete", json=body, headers=_idem_headers(t, key))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/tasks/{tid}/complete", json=body, headers=_idem_headers(t, key))
    assert r2.status_code == 200
    assert r2.json()["data"] == r1.json()["data"]
    # same key, different verification body → conflict
    r3 = await client.post(f"/api/v1/tasks/{tid}/complete",
                           json={"verification": {"required": True, "status": "PENDING"}},
                           headers=_idem_headers(t, key))
    assert r3.status_code == 409


async def test_incident_same_key_different_body_conflict(client, login):
    t = await login(OWNER_EMAIL)
    key = unique_key()
    body1 = {"severity": "HIGH", "system": "api", "issue": "a", "impact": "x"}
    body2 = {"severity": "LOW", "system": "api", "issue": "b", "impact": "y"}
    r1 = await client.post("/api/v1/incidents", json=body1, headers=_idem_headers(t, key))
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/incidents", json=body2, headers=_idem_headers(t, key))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "CONFLICT"
    count = await client.app.state.db.fetchone("SELECT count(*)::int AS n FROM incidents")
    assert count["n"] == 1
