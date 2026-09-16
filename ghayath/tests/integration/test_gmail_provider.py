"""Gmail contract/security tests at the injected transport boundary.

These tests never pretend to be a Google credential or a production provider
call. The real-provider test remains blocked unless an operator configures a
Google OAuth client outside the test suite.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.integrations.gmail import (
    GMAIL_READONLY_SCOPE,
    EncryptedTokenStore,
    GmailHTTPAdapter,
    GmailMessagePage,
    GmailTokens,
    normalize_gmail_message,
)
from app.services.gmail_service import GmailService


def settings() -> Settings:
    return Settings(
        auth_jwt_secret="test-jwt-secret-not-for-production",
        gmail_client_id="client-id",
        gmail_client_secret="client-secret",
        gmail_redirect_uri="https://example.test/api/v1/integrations/gmail/oauth/callback",
        email_token_encryption_key=Fernet.generate_key().decode(),
        _env_file=None,
    )


def full_message(message_id="m_1", thread_id="t_1") -> dict:
    enc = base64.urlsafe_b64encode(b"Hello from Gmail").decode().rstrip("=")
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": "1720000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hello snippet",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Alice Example <alice@example.com>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": "Project update"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": enc}},
                {"filename": "plan.pdf", "mimeType": "application/pdf",
                 "body": {"attachmentId": "att_1", "size": 12}},
            ],
        },
    }


@pytest.mark.asyncio
async def test_oauth_url_is_stateful_and_readonly_and_tokens_are_encrypted():
    provider = GmailHTTPAdapter(settings())
    url = provider.authorization_url("random-state", "owner@example.com")
    query = parse_qs(httpx.URL(url).query.decode())
    assert query["scope"] == [GMAIL_READONLY_SCOPE]
    assert "gmail.send" not in url
    assert query["state"] == ["random-state"]

    token = GmailTokens("access-secret", "refresh-secret", datetime.now(timezone.utc) + timedelta(hours=1))
    store = EncryptedTokenStore(settings().email_token_encryption_key)
    ciphertext = store.encrypt(token)
    assert b"access-secret" not in ciphertext
    assert store.decrypt(ciphertext).refresh_token == "refresh-secret"


@pytest.mark.asyncio
async def test_provider_oauth_exchange_retry_pagination_details_and_revocation():
    state = {"list": 0, "message": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["client-secret"]
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        if request.url.path == "/revoke":
            return httpx.Response(200)
        if request.url.path.endswith("/messages"):
            state["list"] += 1
            if state["list"] == 1:
                return httpx.Response(503, json={"error": "not exposed to callers"})
            return httpx.Response(200, json={"messages": [{"id": "m_1", "threadId": "t_1"}],
                                             "nextPageToken": "next", "resultSizeEstimate": 1})
        if "/messages/m_1" in request.url.path:
            state["message"] += 1
            return httpx.Response(200, json=full_message())
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GmailHTTPAdapter(settings(), client=client, sleep=lambda _: _noop())
    tokens = await provider.exchange_code("one-time-code")
    assert tokens.refresh_token == "refresh"
    page = await provider.list_messages(tokens.access_token)
    assert page.next_page_token == "next"
    normalized = await provider.get_message(tokens.access_token, "m_1")
    assert normalized["message_id"] == "m_1"
    assert normalized["sender"]["email"] == "alice@example.com"
    assert normalized["attachments"][0]["attachment_id"] == "att_1"
    await provider.revoke(tokens.refresh_token or tokens.access_token)
    await client.aclose()


async def _noop():
    return None


@pytest.mark.asyncio
async def test_normalizer_rejects_malformed_provider_response():
    with pytest.raises(Exception):
        normalize_gmail_message({"id": "m_1", "threadId": "t_1"})


class _Integrations:
    def __init__(self, blob):
        self.row = {"id": "gmail", "status": "CONNECTED", "config_enc": blob, "last_checked_at": None}

    async def get(self, _provider):
        return self.row

    async def store_config(self, _provider, blob, status):
        self.row["config_enc"] = blob
        self.row["status"] = status

    async def set_status(self, _provider, status):
        self.row["status"] = status
        self.row["last_checked_at"] = datetime.now(timezone.utc)


class _Email:
    def __init__(self):
        self.rows = {}

    async def get_gmail_message(self, message_id):
        return self.rows.get(message_id)

    async def upsert_gmail(self, **fields):
        self.rows[fields["gmail_message_id"]] = fields
        return "msg_test"


class _DB:
    def __init__(self, blob):
        self.integrations = _Integrations(blob)
        self.email = _Email()


class _Provider:
    def __init__(self):
        self.calls = 0

    def configured(self):
        return True

    async def list_messages(self, *args, **kwargs):
        return GmailMessagePage([{"id": "m_1", "threadId": "t_1"}], None, 1)

    async def get_message(self, _access, _message_id):
        self.calls += 1
        return normalize_gmail_message(full_message())

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_preserves_provider_ids_and_identity_state():
    cfg = settings()
    store = EncryptedTokenStore(cfg.email_token_encryption_key)
    blob = store.encrypt(GmailTokens("access", "refresh", None, account_email="owner@example.com"))
    db = _DB(blob)
    service = GmailService(db, cfg, _Provider())
    first = await service.sync(None, 50, None, "idem-12345678")
    second = await service.sync(None, 50, None, "idem-12345679")
    assert first["imported_count"] == 1
    assert second["skipped_count"] == 1
    assert first["messages"][0]["message_id"] == "m_1"
    assert first["messages"][0]["intelligence"]["identity_status"] == "IDENTITY_UNVERIFIED"
    assert db.email.rows["m_1"]["thread_id"] == "t_1"


@pytest.mark.asyncio
async def test_unconfigured_service_reports_requires_connection_without_empty_success():
    cfg = Settings(auth_jwt_secret="test-jwt-secret-not-for-production", _env_file=None)
    db = _DB(None)
    db.integrations.row["config_enc"] = None
    provider = _Provider()
    provider.configured = lambda: False
    result = await GmailService(db, cfg, provider).status()
    assert result["state"] == "REQUIRES_CONNECTION"
    assert result["email"] is None


@pytest.mark.asyncio
async def test_api_reports_unavailable_gmail_without_fabricating_inbox(client, login):
    token = await login("owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    status = await client.get("/api/v1/integrations/gmail/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["data"]["state"] == "REQUIRES_CONNECTION"

    messages = await client.get("/api/v1/integrations/gmail/messages", headers=headers)
    assert messages.status_code == 503
    assert messages.json()["error"]["code"] == "INTEGRATION_OFFLINE"

    sync = await client.post("/api/v1/integrations/gmail/sync", headers={**headers, "Idempotency-Key": "gmail-test-123456"})
    assert sync.status_code == 503
    assert sync.json()["error"]["code"] == "INTEGRATION_OFFLINE"


@pytest.mark.asyncio
async def test_api_oauth_start_requires_owner_connection_and_callback_state(client, login):
    token = await login("owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    start = await client.get("/api/v1/integrations/gmail/oauth/start", headers=headers)
    assert start.status_code == 503
    assert start.json()["error"]["code"] == "INTEGRATION_OFFLINE"
    callback = await client.get("/api/v1/integrations/gmail/oauth/callback?state=not-a-real-state&code=x")
    assert callback.status_code == 400
    assert callback.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_gmail_mapping_upsert_uses_provider_id_and_unified_inbox_db(client):
    db = client.app.state.db
    await db.email.upsert_gmail(
        gmail_message_id="real_provider_message_1", thread_id="real_provider_thread_1",
        sender="alice@example.com", subject="A", body="B", priority="NORMAL",
        classification="UNKNOWN", received_at=datetime.now(timezone.utc), labels=["SPAM"],
        headers={"from": "alice@example.com"}, attachments=[], snippet="B", folder="spam")
    row = await db.email.get_gmail_message("real_provider_message_1")
    assert row is not None
    assert row["external_id"] == "real_provider_message_1"
    assert row["thread_id"] == "real_provider_thread_1"
    assert row["folder"] == "spam"
    await db.email.upsert_gmail(
        gmail_message_id="real_provider_message_1", thread_id="real_provider_thread_1",
        sender="alice@example.com", subject="A2", body="B2", priority="NORMAL",
        classification="UNKNOWN", received_at=datetime.now(timezone.utc), labels=["INBOX"],
        headers={}, attachments=[], snippet="B2", folder="inbox")
    updated = await db.email.get_gmail_message("real_provider_message_1")
    assert updated["subject"] == "A2"
    assert updated["folder"] == "inbox"
