"""Standalone Gmail adapter and OAuth/token primitives.

This module owns Google wire formats and HTTP behavior only. Routes, services,
repositories, and the agent never construct Gmail requests themselves. The
transport is injectable for tests, but production always uses Google's real
OAuth and Gmail endpoints; there is no fake provider fallback.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailProviderError(Exception):
    """Safe provider exception; it intentionally excludes provider response text."""

    def __init__(self, kind: str, status: int | None = None, retryable: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.retryable = retryable


class GmailCredentialsMissing(GmailProviderError):
    def __init__(self):
        super().__init__("credentials_missing")


class GmailAuthorizationError(GmailProviderError):
    def __init__(self, kind: str = "authorization_failed", status: int | None = None):
        super().__init__(kind, status=status)


class GmailAuthenticationExpired(GmailProviderError):
    def __init__(self):
        super().__init__("access_token_expired", status=401)


class GmailMalformedResponse(GmailProviderError):
    def __init__(self):
        super().__init__("malformed_provider_response")


class GmailProvider(Protocol):
    """Provider interface consumed by GmailService, independent of FastAPI."""

    def configured(self) -> bool: ...
    def authorization_url(self, state: str, login_hint: str | None = None) -> str: ...
    async def exchange_code(self, code: str) -> "GmailTokens": ...
    async def refresh(self, refresh_token: str) -> "GmailTokens": ...
    async def revoke(self, token: str) -> None: ...
    async def list_messages(self, access_token: str, page_token: str | None = None,
                            max_results: int = 100, query: str | None = None,
                            label_ids: list[str] | None = None) -> "GmailMessagePage": ...
    async def get_message(self, access_token: str, message_id: str) -> dict[str, Any]: ...
    async def get_thread(self, access_token: str, thread_id: str) -> list[dict[str, Any]]: ...
    async def get_profile(self, access_token: str) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class GmailTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    token_type: str = "Bearer"
    scope: str = GMAIL_READONLY_SCOPE
    account_email: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "token_type": self.token_type,
            "scope": self.scope,
            "account_email": self.account_email,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GmailTokens":
        access = value.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError("missing access token")
        raw_expiry = value.get("expires_at")
        expiry = datetime.fromisoformat(raw_expiry) if isinstance(raw_expiry, str) else None
        refresh = value.get("refresh_token")
        if refresh is not None and not isinstance(refresh, str):
            raise ValueError("invalid refresh token")
        token_type = value.get("token_type") or "Bearer"
        scope = value.get("scope") or GMAIL_READONLY_SCOPE
        if not isinstance(token_type, str) or not isinstance(scope, str):
            raise ValueError("invalid token metadata")
        if any(item != GMAIL_READONLY_SCOPE for item in scope.split()):
            raise ValueError("unsupported Gmail scope")
        return cls(access, refresh, expiry, token_type, scope,
                   value.get("account_email") if isinstance(value.get("account_email"), str) else None)


@dataclass(frozen=True)
class GmailMessagePage:
    messages: list[dict[str, str]]
    next_page_token: str | None
    result_size_estimate: int | None


class EncryptedTokenStore:
    """Fernet envelope for OAuth material; plaintext never reaches the database."""

    def __init__(self, key: str | bytes | None):
        if not key:
            raise ValueError("token encryption key is not configured")
        raw = key.encode() if isinstance(key, str) else key
        try:
            self._fernet = Fernet(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("token encryption key is invalid") from exc

    def encrypt(self, tokens: GmailTokens) -> bytes:
        return self._fernet.encrypt(json.dumps(tokens.as_dict(), separators=(",", ":")).encode())

    def decrypt(self, blob: bytes | str) -> GmailTokens:
        try:
            raw = self._fernet.decrypt(blob.encode() if isinstance(blob, str) else blob)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return GmailTokens.from_dict(value)
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("encrypted token data is invalid") from exc


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class GmailHTTPAdapter:
    """Google OAuth + Gmail REST adapter with bounded transient retries."""

    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None,
                 sleep=asyncio.sleep):
        self._settings = settings
        self._client = client
        self._sleep = sleep

    def configured(self) -> bool:
        return bool(self._settings.gmail_client_id and self._settings.gmail_client_secret and
                    self._settings.gmail_redirect_uri)

    def authorization_url(self, state: str, login_hint: str | None = None) -> str:
        if not self.configured():
            raise GmailCredentialsMissing()
        params = {
            "client_id": self._settings.gmail_client_id,
            "redirect_uri": self._settings.gmail_redirect_uri,
            "response_type": "code",
            "scope": GMAIL_READONLY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return self._settings.google_oauth_base_url.rstrip("/") + "/auth?" + urlencode(params)

    async def exchange_code(self, code: str) -> GmailTokens:
        if not self.configured():
            raise GmailCredentialsMissing()
        data = await self._oauth_post("/token", {
            "code": code,
            "client_id": self._settings.gmail_client_id,
            "client_secret": self._settings.gmail_client_secret,
            "redirect_uri": self._settings.gmail_redirect_uri,
            "grant_type": "authorization_code",
        })
        return _tokens_from_oauth(data, require_refresh=False)

    async def refresh(self, refresh_token: str) -> GmailTokens:
        if not self.configured():
            raise GmailCredentialsMissing()
        data = await self._oauth_post("/token", {
            "refresh_token": refresh_token,
            "client_id": self._settings.gmail_client_id,
            "client_secret": self._settings.gmail_client_secret,
            "grant_type": "refresh_token",
        })
        return _tokens_from_oauth(data, require_refresh=False, fallback_refresh=refresh_token)

    async def revoke(self, token: str) -> None:
        if not self.configured():
            raise GmailCredentialsMissing()
        response = await self._request("POST", self._settings.google_oauth_base_url.rstrip("/") + "/revoke",
                                       data={"token": token}, oauth=True)
        if response.status_code >= 400:
            raise GmailProviderError("revocation_failed", response.status_code,
                                     response.status_code in self._RETRY_STATUS)

    async def list_messages(self, access_token: str, page_token: str | None = None,
                            max_results: int = 100, query: str | None = None,
                            label_ids: list[str] | None = None) -> GmailMessagePage:
        params: dict[str, Any] = {"maxResults": max_results}
        if label_ids:
            params["labelIds"] = label_ids
        if page_token:
            params["pageToken"] = page_token
        if query:
            params["q"] = query
        data = await self._gmail_request("GET", "/users/me/messages", access_token, params=params)
        messages = data.get("messages", [])
        if messages is None:
            messages = []
        if not isinstance(messages, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str)
                                                 for m in messages):
            raise GmailMalformedResponse()
        return GmailMessagePage(
            [{"id": m["id"], "threadId": m.get("threadId", "")} for m in messages],
            data.get("nextPageToken") if isinstance(data.get("nextPageToken"), str) else None,
            data.get("resultSizeEstimate") if isinstance(data.get("resultSizeEstimate"), int) else None,
        )

    async def get_message(self, access_token: str, message_id: str) -> dict[str, Any]:
        data = await self._gmail_request("GET", f"/users/me/messages/{message_id}", access_token,
                                         params={"format": "full"})
        return normalize_gmail_message(data)

    async def get_thread(self, access_token: str, thread_id: str) -> list[dict[str, Any]]:
        data = await self._gmail_request("GET", f"/users/me/threads/{thread_id}", access_token,
                                         params={"format": "full"})
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise GmailMalformedResponse()
        return [normalize_gmail_message(item) for item in messages]

    async def get_profile(self, access_token: str) -> dict[str, Any]:
        data = await self._gmail_request("GET", "/users/me/profile", access_token)
        if not isinstance(data.get("emailAddress"), str):
            raise GmailMalformedResponse()
        return {"email": data["emailAddress"], "messages_total": data.get("messagesTotal")}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _oauth_post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", self._settings.google_oauth_base_url.rstrip("/") + path,
                                       data=data, oauth=True)
        if response.status_code >= 400:
            if response.status_code in (400, 401):
                raise GmailAuthorizationError("oauth_exchange_failed", response.status_code)
            raise GmailProviderError("oauth_provider_unavailable", response.status_code,
                                     response.status_code in self._RETRY_STATUS)
        return _json_object(response)

    async def _gmail_request(self, method: str, path: str, access_token: str, **kwargs) -> dict[str, Any]:
        response = await self._request(method, self._settings.gmail_api_base_url.rstrip("/") + path,
                                       headers={"Authorization": f"Bearer {access_token}"}, **kwargs)
        if response.status_code == 401:
            raise GmailAuthenticationExpired()
        if response.status_code >= 400:
            raise GmailProviderError("gmail_provider_error", response.status_code,
                                     response.status_code in self._RETRY_STATUS)
        return _json_object(response)

    async def _request(self, method: str, url: str, *, oauth: bool = False, **kwargs) -> httpx.Response:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            for attempt in range(3):
                try:
                    response = await client.request(method, url, **kwargs)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt == 2:
                        raise GmailProviderError("provider_unavailable", retryable=True) from exc
                    await self._sleep(0)
                    continue
                if response.status_code not in self._RETRY_STATUS or attempt == 2:
                    return response
                retry_after = response.headers.get("Retry-After", "0")
                try:
                    delay = min(float(retry_after), 2.0)
                except ValueError:
                    delay = 0.0
                await self._sleep(max(0.0, delay))
            raise GmailProviderError("provider_unavailable", retryable=True)
        finally:
            if owned:
                await client.aclose()


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GmailMalformedResponse() from exc
    if not isinstance(value, dict):
        raise GmailMalformedResponse()
    return value


def _tokens_from_oauth(data: dict[str, Any], require_refresh: bool,
                       fallback_refresh: str | None = None) -> GmailTokens:
    access = data.get("access_token")
    refresh = data.get("refresh_token") or fallback_refresh
    if not isinstance(access, str) or not access or (require_refresh and not isinstance(refresh, str)):
        raise GmailMalformedResponse()
    expires = data.get("expires_in")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires)) if isinstance(expires, (int, float)) else None
    scope: str = GMAIL_READONLY_SCOPE
    raw_scope = data.get("scope")
    if isinstance(raw_scope, str):
        scope = raw_scope
    if any(item != GMAIL_READONLY_SCOPE for item in scope.split()):
        raise GmailMalformedResponse()
    token_type: str = "Bearer"
    raw_token_type = data.get("token_type")
    if isinstance(raw_token_type, str):
        token_type = raw_token_type
    refresh_token = refresh if isinstance(refresh, str) else None
    return GmailTokens(access, refresh_token, expires_at, token_type, scope)


def _decode_body(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return ""


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("headers")
    if not isinstance(raw, list):
        return {}
    result: dict[str, str] = {}
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
            result[item["name"].lower()] = item["value"]
    return result


def _addresses(value: str | None) -> list[dict[str, str]]:
    # Gmail normally supplies RFC 5322 addresses. Invalid provider values are
    # ignored rather than allowed to become an unsafe identity claim.
    return [{"name": name, "email": address} for name, address in getaddresses([value or ""])
            if address and "@" in address]


def _parts(payload: dict[str, Any]):
    yield payload
    children = payload.get("parts")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                yield from _parts(child)


def _body_and_attachments(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    body = ""
    attachments: list[dict[str, Any]] = []
    for part in _parts(payload):
        filename = part.get("filename")
        body_data = ((part.get("body") or {}).get("data") if isinstance(part.get("body"), dict) else None)
        mime = part.get("mimeType")
        if isinstance(filename, str) and filename:
            item: dict[str, Any] = {"filename": filename, "mime_type": mime or "application/octet-stream"}
            part_body = part.get("body") if isinstance(part.get("body"), dict) else {}
            if isinstance(part_body.get("attachmentId"), str):
                item["attachment_id"] = part_body["attachmentId"]
            if isinstance(part_body.get("size"), int):
                item["size"] = part_body["size"]
            attachments.append(item)
        elif not body and isinstance(body_data, str) and mime in ("text/plain", "text/html"):
            body = _decode_body(body_data)
    return body, attachments


def normalize_gmail_message(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Gmail full-message JSON to the provider-neutral inbox shape."""
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("threadId"), str):
        raise GmailMalformedResponse()
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise GmailMalformedResponse()
    headers = _headers(payload)
    body, attachments = _body_and_attachments(payload)
    internal = data.get("internalDate")
    internal_value = internal if isinstance(internal, (int, str)) else None
    timestamp = None
    if internal_value is not None:
        try:
            timestamp = datetime.fromtimestamp(int(internal_value) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            timestamp = None
    senders = _addresses(headers.get("from"))
    return {
        "message_id": data["id"],
        "thread_id": data["threadId"],
        "sender": senders[0] if senders else None,
        "recipients": _addresses(", ".join(x for x in (headers.get("to"), headers.get("cc")) if x)),
        "subject": headers.get("subject", ""),
        "timestamp": timestamp,
        "labels": data.get("labelIds") if isinstance(data.get("labelIds"), list) else [],
        "body": body,
        "snippet": data.get("snippet") if isinstance(data.get("snippet"), str) else "",
        "attachments": attachments,
        "history_id": data.get("historyId") if isinstance(data.get("historyId"), str) else None,
        "headers": headers,
    }
