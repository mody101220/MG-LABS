"""Authentication service (contract §3): login, refresh (rotation + reuse detection), logout."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import jwt

from app.core import context
from app.core.config import Settings
from app.core.errors import AppError, authentication_failed, invalid_token
from app.core.security import create_access_token, verify_password
from app.generated import models
from app.services.audit_service import AuditService
from app.services.ids import new_id


class AuthService:
    def __init__(self, db, settings: Settings, audit: AuditService) -> None:
        self._db = db
        self._settings = settings
        self._audit = audit

    def _tokens(self, user: dict) -> tuple[str, str]:
        access = create_access_token(user["id"], user["role"], self._settings.auth_jwt_secret,
                                     self._settings.access_token_ttl_seconds)
        refresh_raw = secrets.token_urlsafe(48)
        return access, refresh_raw

    def _refresh_hash(self, refresh_raw: str) -> str:
        return hashlib.sha256(refresh_raw.encode()).hexdigest()

    async def login(self, email: str, password: str) -> dict:
        # The generated LoginRequest types password as pydantic SecretStr (format: password).
        if hasattr(password, "get_secret_value"):
            password = password.get_secret_value()
        user = await self._db.users.get_by_email(email)
        if user is None or not user["is_active"] or not verify_password(password, user["password_hash"]):
            await self._audit.log("LOGIN_FAILED", None, "FAILURE", "user", None,
                                  details={"email": email, "reason": "bad_credentials"})
            raise authentication_failed("Email or password is incorrect")
        access, refresh_raw = self._tokens(user)
        await self._db.tokens.create(new_id("tok"), user["id"], self._refresh_hash(refresh_raw),
                                     context.now_utc() + timedelta(days=self._settings.refresh_token_ttl_days))
        await self._audit.log("LOGIN", None, "SUCCESS", "user", user["id"])
        return {
            "access_token": access,
            "refresh_token": refresh_raw,
            "token_type": models.TokenType.Bearer,
            "expires_in": self._settings.access_token_ttl_seconds,
            "user": {"id": user["id"], "email": user["email"], "role": user["role"]},
        }

    async def refresh(self, refresh_raw: str) -> dict:
        row = await self._db.tokens.find_by_hash(self._refresh_hash(refresh_raw))
        if row is None:
            raise invalid_token("Refresh token is invalid or expired")
        if row["revoked_at"] is not None:
            # Reuse of a rotated token → treat as theft: revoke the whole session family.
            await self._db.tokens.revoke_all_for_user(row["user_id"])
            raise invalid_token("Refresh token reuse detected; session revoked")
        if row["expires_at"] <= context.now_utc():
            raise invalid_token("Refresh token is invalid or expired")
        user = await self._db.users.get_by_id(row["user_id"])
        if user is None or not user["is_active"]:
            raise invalid_token("User is disabled")
        await self._db.tokens.revoke(row["token_hash"])
        access, refresh_raw = self._tokens(user)
        await self._db.tokens.create(new_id("tok"), user["id"], self._refresh_hash(refresh_raw),
                                     context.now_utc() + timedelta(days=self._settings.refresh_token_ttl_days))
        return {
            "access_token": access,
            "refresh_token": refresh_raw,
            "token_type": models.TokenType.Bearer,
            "expires_in": self._settings.access_token_ttl_seconds,
            "user": {"id": user["id"], "email": user["email"], "role": user["role"]},
        }

    async def logout(self, user_id: str, raw_token: str | None = None) -> None:
        await self._db.tokens.revoke_all_for_user(user_id)
        if raw_token:
            try:
                from app.core.security import decode_access_token

                _uid, _role, jti = decode_access_token(raw_token, self._settings.auth_jwt_secret)
                if jti:
                    await self._db.tokens.revoke_access_jti(user_id, jti, self._settings.access_token_ttl_seconds)
            except AppError:
                pass  # token already invalid; refresh family is revoked regardless
        await self._audit.log("LOGOUT", None, "SUCCESS", "user", user_id)

    @staticmethod
    def decode_token_payload(token: str) -> dict:
        return jwt.decode(token, options={"verify_signature": False})
