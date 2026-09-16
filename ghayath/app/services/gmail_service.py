"""Gmail application service: OAuth lifecycle, normalized reads, and idempotent sync."""
from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError, integration_offline, not_found, tool_unavailable, validation_error
from app.core.security import Principal
from app.integrations.gmail import (
    EncryptedTokenStore,
    GmailAuthenticationExpired,
    GmailAuthorizationError,
    GmailCredentialsMissing,
    GmailHTTPAdapter,
    GmailMalformedResponse,
    GmailProvider,
    GmailProviderError,
    GmailTokens,
    hash_oauth_state,
)
from app.services.classification import classify_message


class GmailService:
    def __init__(self, db, settings: Settings, provider: GmailProvider | None = None, audit=None) -> None:
        self._db = db
        self._settings = settings
        self._provider = provider or GmailHTTPAdapter(settings)
        self._audit = audit

    def _token_store(self) -> EncryptedTokenStore:
        try:
            return EncryptedTokenStore(self._settings.email_token_encryption_key)
        except ValueError:
            raise integration_offline("gmail")

    async def status(self) -> dict[str, Any]:
        row = await self._db.integrations.get("gmail")
        last_sync = row.get("last_checked_at") if row else None
        last_sync_reader = getattr(self._db.email, "last_gmail_sync_at", None)
        if last_sync_reader is not None:
            last_sync = await last_sync_reader()
        if not row or not row.get("config_enc") or not self._provider.configured():
            return {"provider": "gmail", "state": "REQUIRES_CONNECTION", "email": None,
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                    "expires_at": None, "last_sync_at": last_sync}
        try:
            tokens = self._token_store().decrypt(row["config_enc"])
        except ValueError:
            return {"provider": "gmail", "state": "DEGRADED", "email": None,
                    "scopes": [], "expires_at": None, "last_sync_at": last_sync}
        state = row.get("status") if row.get("status") in {"CONNECTED", "DEGRADED"} else "CONNECTED"
        return {"provider": "gmail", "state": state, "email": tokens.account_email,
                "scopes": [tokens.scope], "expires_at": tokens.expires_at,
                "last_sync_at": last_sync}

    async def start_oauth(self, principal: Principal) -> dict[str, Any]:
        if not principal.is_owner:
            from app.core.errors import forbidden
            raise forbidden(required="OWNER")
        if not self._provider.configured():
            raise integration_offline("gmail")
        self._token_store()  # fail closed before creating state if encryption is absent/invalid
        state = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=self._settings.gmail_oauth_state_ttl_seconds)
        await self._db.gmail_oauth.create(hash_oauth_state(state), principal.user_id, expires)
        try:
            url = self._provider.authorization_url(state, principal.email)
        except GmailCredentialsMissing as exc:
            raise integration_offline("gmail") from exc
        return {"authorization_url": url, "expires_at": expires}

    async def complete_oauth(self, state: str, code: str | None, error: str | None = None) -> dict[str, Any]:
        if not state or len(state) > 512:
            raise validation_error({"field": "state", "reason": "invalid OAuth state"})
        owner = await self._db.gmail_oauth.consume(hash_oauth_state(state))
        if not owner:
            raise validation_error({"field": "state", "reason": "expired or already used OAuth state"})
        audit_principal = None
        user = await self._db.users.get_by_id(owner["user_id"])
        if user:
            audit_principal = Principal(user["id"], user["email"], user["role"])
        if error:
            await self._audit_event("GMAIL_OAUTH_CALLBACK", "FAILURE", {"reason": "provider_denied"}, audit_principal)
            return {"status": "FAILED", "email": None}
        if not code:
            raise validation_error({"field": "code", "reason": "authorization code is required"})
        try:
            tokens = await self._provider.exchange_code(code)
            profile = await self._provider.get_profile(tokens.access_token)
            tokens = replace(tokens, account_email=profile.get("email"))
            await self._db.integrations.store_config("gmail", self._token_store().encrypt(tokens), "CONNECTED")
        except GmailCredentialsMissing as exc:
            raise integration_offline("gmail") from exc
        except GmailAuthorizationError as exc:
            await self._audit_event("GMAIL_OAUTH_CALLBACK", "FAILURE", {"reason": "authorization_failed"}, audit_principal)
            raise validation_error({"field": "code", "reason": "Google authorization could not be completed"}) from exc
        except (GmailProviderError, ValueError) as exc:
            await self._audit_event("GMAIL_OAUTH_CALLBACK", "FAILURE", {"reason": "provider_unavailable"}, audit_principal)
            raise tool_unavailable("gmail.oauth") from exc
        await self._audit_event("GMAIL_OAUTH_CALLBACK", "SUCCESS", {}, audit_principal)
        return {"status": "CONNECTED", "email": tokens.account_email}

    async def revoke(self, principal: Principal) -> None:
        row, tokens = await self._load_tokens()
        try:
            await self._provider.revoke(tokens.refresh_token or tokens.access_token)
        except GmailProviderError as exc:
            raise tool_unavailable("gmail.revoke") from exc
        await self._db.integrations.clear_config("gmail")
        await self._audit_event("GMAIL_REVOKE", "SUCCESS", {}, principal)

    async def list_messages(self, page_token: str | None, max_results: int, query: str | None,
                            label_ids: list[str] | None) -> dict[str, Any]:
        tokens = await self._usable_tokens()
        try:
            page = await self._provider.list_messages(tokens.access_token, page_token, max_results, query, label_ids)
        except GmailAuthenticationExpired:
            tokens = await self._refresh_tokens(tokens)
            page = await self._provider.list_messages(tokens.access_token, page_token, max_results, query, label_ids)
        except GmailProviderError as exc:
            raise self._map_provider_error(exc, "gmail.read") from exc
        messages = []
        for item in page.messages:
            try:
                messages.append(await self._detail(tokens, item["id"]))
            except GmailProviderError as exc:
                raise self._map_provider_error(exc, "gmail.read") from exc
        return {"messages": messages, "next_page_token": page.next_page_token,
                "result_size_estimate": page.result_size_estimate}

    async def get_message(self, message_id: str) -> dict[str, Any]:
        tokens = await self._usable_tokens()
        try:
            value = await self._provider.get_message(tokens.access_token, message_id)
        except GmailAuthenticationExpired:
            tokens = await self._refresh_tokens(tokens)
            value = await self._provider.get_message(tokens.access_token, message_id)
        except GmailProviderError as exc:
            raise self._map_provider_error(exc, "gmail.read", not_found_on_404=True) from exc
        return self._public_message(value)

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        tokens = await self._usable_tokens()
        try:
            values = await self._provider.get_thread(tokens.access_token, thread_id)
        except GmailAuthenticationExpired:
            tokens = await self._refresh_tokens(tokens)
            values = await self._provider.get_thread(tokens.access_token, thread_id)
        except GmailProviderError as exc:
            raise self._map_provider_error(exc, "gmail.read", not_found_on_404=True) from exc
        return {"thread_id": thread_id, "messages": [self._public_message(v) for v in values]}

    async def sync(self, page_token: str | None, max_results: int, query: str | None,
                   idempotency_key: str, principal: Principal | None = None) -> dict[str, Any]:
        # A page token is caller-provided and each message is upserted by the
        # original Gmail messageId. A retried page is therefore safe.
        page = await self.list_messages(page_token, max_results, query, None)
        imported = 0
        skipped = 0
        for public in page["messages"]:
            existing = await self._db.email.get_gmail_message(public["message_id"])
            normalized = self._sync_fields(public)
            await self._db.email.upsert_gmail(**normalized)
            if existing:
                skipped += 1
            else:
                imported += 1
        if page["messages"]:
            row = await self._db.integrations.get("gmail")
            if row:
                await self._db.integrations.set_status("gmail", "CONNECTED")
        await self._audit_event("GMAIL_SYNC", "SUCCESS",
                                {"imported_count": imported, "skipped_count": skipped, "idempotency_key": idempotency_key},
                                principal)
        return {"imported_count": imported, "skipped_count": skipped,
                "next_page_token": page["next_page_token"], "messages": page["messages"]}

    async def close(self) -> None:
        await self._provider.close()

    async def _usable_tokens(self) -> GmailTokens:
        _, tokens = await self._load_tokens()
        if tokens.expires_at and tokens.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=30):
            return await self._refresh_tokens(tokens)
        return tokens

    async def _load_tokens(self):
        row = await self._db.integrations.get("gmail")
        if not row or not row.get("config_enc"):
            raise integration_offline("gmail")
        try:
            return row, self._token_store().decrypt(row["config_enc"])
        except ValueError as exc:
            raise integration_offline("gmail") from exc

    async def _refresh_tokens(self, tokens: GmailTokens) -> GmailTokens:
        if not tokens.refresh_token:
            raise integration_offline("gmail")
        try:
            refreshed = await self._provider.refresh(tokens.refresh_token)
        except GmailProviderError as exc:
            raise self._map_provider_error(exc, "gmail.refresh") from exc
        refreshed = replace(refreshed, account_email=tokens.account_email)
        await self._db.integrations.store_config("gmail", self._token_store().encrypt(refreshed), "CONNECTED")
        return refreshed

    async def _detail(self, tokens: GmailTokens, message_id: str) -> dict[str, Any]:
        try:
            return self._public_message(await self._provider.get_message(tokens.access_token, message_id))
        except GmailAuthenticationExpired:
            tokens = await self._refresh_tokens(tokens)
            return self._public_message(await self._provider.get_message(tokens.access_token, message_id))

    @staticmethod
    def _public_message(value: dict[str, Any]) -> dict[str, Any]:
        sender = value.get("sender") or {}
        sender_email = sender.get("email") if isinstance(sender, dict) else None
        sender_display = sender_email or (sender if isinstance(sender, str) else None)
        recipients = []
        for recipient in value.get("recipients", []):
            if isinstance(recipient, dict):
                recipients.append(recipient.get("email") or "")
            elif isinstance(recipient, str):
                recipients.append(recipient)
        attachments = []
        for item in value.get("attachments", []):
            if isinstance(item, dict):
                attachments.append({"filename": str(item.get("filename") or ""),
                                    "mime_type": str(item.get("mime_type") or "application/octet-stream"),
                                    "size": int(item.get("size") or 0),
                                    "attachment_id": item.get("attachment_id")})
        classification = classify_message(f"{value.get('subject') or ''} {value.get('snippet') or ''} {value.get('body') or ''}")
        identity = "IDENTITY_UNVERIFIED" if sender_email else "UNKNOWN"
        return {
            "message_id": value.get("message_id"), "thread_id": value.get("thread_id"),
            "sender": sender_display, "recipients": [r for r in recipients if r],
            "subject": value.get("subject"), "timestamp": value.get("timestamp"),
            "labels": value.get("labels") or [], "snippet": value.get("snippet"),
            "body": value.get("body"), "attachments": attachments,
            "intelligence": {"channel": "EMAIL", "intent": "UNKNOWN", "priority": classification,
                             "status": "RECEIVED", "related_project": "UNKNOWN",
                             "related_customer": "UNKNOWN", "next_action": "UNKNOWN",
                             "deadline": None, "identity_status": identity,
                             "contact_id": None, "crm_id": None},
        }

    @classmethod
    def _sync_fields(cls, public: dict[str, Any]) -> dict[str, Any]:
        timestamp = public.get("timestamp")
        received = None
        if timestamp:
            try:
                received = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                received = None
        intelligence = public["intelligence"]
        return {
            "gmail_message_id": public["message_id"], "thread_id": public["thread_id"],
            "sender": public.get("sender"), "subject": public.get("subject"),
            "body": public.get("body"), "priority": "URGENT" if intelligence["priority"] == "URGENT" else "NORMAL",
            "classification": intelligence["priority"], "received_at": received,
            "labels": public.get("labels") or [], "headers": {},
            "attachments": public.get("attachments") or [], "snippet": public.get("snippet"),
            "folder": _gmail_folder(public.get("labels") or []),
        }

    async def _audit_event(self, action: str, result: str, details: dict[str, Any],
                           principal: Principal | None = None) -> None:
        if self._audit:
            await self._audit.log(action, principal, result, target_type="gmail", target_id="gmail", details=details)

    @staticmethod
    def _map_provider_error(exc: GmailProviderError, tool: str, not_found_on_404: bool = False) -> AppError:
        if not_found_on_404 and exc.status == 404:
            return not_found("Gmail resource")
        if isinstance(exc, GmailMalformedResponse):
            return tool_unavailable(tool)
        if exc.status in (401, 403):
            return tool_unavailable(tool)
        return tool_unavailable(tool)


def _gmail_folder(labels: list[str]) -> str:
    label_set = {str(label).upper() for label in labels}
    if "SPAM" in label_set:
        return "spam"
    if "TRASH" in label_set:
        return "trash"
    if "SENT" in label_set:
        return "sent"
    if "ARCHIVE" in label_set or "INBOX" not in label_set:
        return "archive"
    return "inbox"
