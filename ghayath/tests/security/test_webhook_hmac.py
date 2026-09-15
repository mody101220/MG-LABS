"""Validation 14: WhatsApp webhook HMAC-SHA256 verification, replay/dedupe,
allow-list, and 'never process unverified payloads'."""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.conftest import WEBHOOK_SECRET

PAYLOAD = {
    "event": "message.received",
    "message_id": "wamid.HBgMtest001",
    "sender": "+963930123456",
    "text": "Please send the invoice",
    "timestamp": "2026-09-15T10:00:00Z",
}


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@asynccontextmanager
async def alt_client(db_reset, **updates):
    """Second app on the same test DB with custom settings (allow-list, no secret)."""
    from app.main import create_app
    from tests.conftest import make_settings

    settings = make_settings(db_reset).model_copy(update=updates)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.app = app
            yield c


def _post(client, payload: dict, signature: str | None = None, secret: str = WEBHOOK_SECRET):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature if signature != "WRONG" else sign(b"some-other-body")
        if signature == "WRONG":
            headers["X-Webhook-Signature"] = sign(b"some-other-body")
    return raw, headers


async def test_valid_signature_accepted(client):
    raw, headers = _post(client, PAYLOAD, sign(json.dumps(PAYLOAD).encode()))
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["success"] is True
    d = body["data"]
    assert d["status"] == "ACCEPTED"
    assert d["message_id"].startswith("wmsg_")
    row = await client.app.state.db.fetchone(
        "SELECT * FROM whatsapp_messages WHERE external_id = %s", PAYLOAD["message_id"])
    assert row is not None
    assert row["status"] == "DELIVERED"
    assert row["classification"] in ("URGENT", "CLIENT", "PERSONAL", "BUSINESS", "PROJECT", "SPAM", "UNKNOWN")


async def test_missing_signature_rejected_and_not_processed(client):
    raw, headers = _post(client, PAYLOAD, None)
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    row = await client.app.state.db.fetchone(
        "SELECT id FROM whatsapp_messages WHERE external_id = %s", PAYLOAD["message_id"])
    assert row is None, "unverified payload must never be processed"


async def test_invalid_signature_rejected(client):
    raw = json.dumps(PAYLOAD).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": "0" * 64}
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 401


async def test_signature_over_wrong_body_rejected(client):
    raw = json.dumps(PAYLOAD).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(b'{"tampered": true}')}
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 401


async def test_wrong_secret_rejected(client, db_reset):
    async with alt_client(db_reset) as c:
        raw = json.dumps(PAYLOAD).encode()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw, "other-secret")}
        r = await c.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
        assert r.status_code == 401


async def test_malformed_json_with_valid_signature(client):
    raw = b"{not json"
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("drop", ["event", "message_id", "sender", "text", "timestamp"])
async def test_contract_required_fields_enforced(client, drop):
    p = dict(PAYLOAD)
    del p[drop]
    raw = json.dumps(p).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
    r = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 400, f"missing {drop} must be 400, got {r.status_code}"


async def test_replay_same_wamid_deduplicated(client):
    raw = json.dumps(PAYLOAD).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
    r1 = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r1.status_code == 202
    r2 = await client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
    assert r2.status_code == 202
    count = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM whatsapp_messages WHERE external_id = %s", PAYLOAD["message_id"])
    assert count["n"] == 1
    # events published exactly once
    ev = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM events WHERE payload->>'message_id' = %s",
        r1.json()["data"]["message_id"])
    assert ev["n"] == 1


async def test_sender_allow_list(client, db_reset):
    payload = dict(PAYLOAD, sender="+9639111222333")
    async with alt_client(db_reset, whatsapp_allowed_senders="+963999999999") as c:
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
        r = await c.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["details"]["reason"] == "sender_not_allowed"
    async with alt_client(db_reset, whatsapp_allowed_senders="+9639111222333") as c:
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
        r = await c.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
        assert r.status_code == 202


async def test_no_webhook_secret_means_503(client, db_reset):
    async with alt_client(db_reset, whatsapp_webhook_secret=None) as c:
        raw = json.dumps(PAYLOAD).encode()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(raw)}
        r = await c.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "INTEGRATION_OFFLINE"
