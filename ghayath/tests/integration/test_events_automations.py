"""Event bus + automation trigger pipeline (contract §16): webhook event triggers
EVENT automation; automation run is audited; SCHEDULE automations get a next_run_at."""
from __future__ import annotations

import hashlib
import hmac
import json

from tests.conftest import OWNER_EMAIL, WEBHOOK_SECRET, bearer


def _signed(payload: dict) -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Content-Type": "application/json", "X-Webhook-Signature": sig}


async def test_event_automation_fires_on_webhook(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/automations", headers=bearer(t), json={
        "name": "Notify on urgent message",
        "trigger": {"type": "EVENT", "event": "message.received"},
        "action": {"type": "SEND_NOTIFICATION", "target": None,
                   "params": {"severity": "LOW", "title": "Inbound WA", "message": "New message"}},
    })
    assert r.status_code == 201, r.text
    aid = r.json()["data"]["id"]

    payload = {
        "event": "message.received",
        "message_id": "wamid.HBgMauto001",
        "sender": "+963930123456",
        "text": "hello",
        "timestamp": "2026-09-15T10:00:00Z",
    }
    raw, headers = _signed(payload)
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 202, r.text

    # the automation ran (audit + last_run)
    row = await client.app.state.db.fetchone("SELECT * FROM automations WHERE id = %s", aid)
    assert row["last_run_status"] == "SUCCESS"
    # a notification was created by the automation
    n = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM notifications WHERE title = 'Inbound WA'")
    assert n["n"] == 1
    ev = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM events WHERE event = 'automation.triggered' AND payload->>'automation_id' = %s", aid)
    assert ev["n"] == 1


async def test_schedule_automation_next_run_computed(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/automations", headers=bearer(t), json={
        "name": "Daily 9am",
        "trigger": {"type": "SCHEDULE", "cron": "0 9 * * *"},
        "action": {"type": "RUN_CHECK", "target": "svc-api", "params": {"target_type": "SERVICE", "checks": ["DATABASE"]}},
    })
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["next_run_at"] is not None


async def test_invalid_cron_rejected(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/automations", headers=bearer(t), json={
        "name": "bad cron",
        "trigger": {"type": "SCHEDULE", "cron": "not a cron"},
        "action": {"type": "CREATE_TASK", "params": {}},
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_invalid_action_type_rejected(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/automations", headers=bearer(t), json={
        "name": "bad action",
        "trigger": {"type": "MANUAL"},
        "action": {"type": "LAUNCH_MISILES"},
    })
    assert r.status_code == 400
