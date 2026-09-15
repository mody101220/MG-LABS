"""Bearer JWT authentication + three-role RBAC (contract: OWNER / AGENT / VIEWER).

- OWNER: full contract access, decides approvals, only role that can authorize high-impact actions.
- AGENT: service identity only. Bound by stored permission bits; cannot grant itself
  permissions (no mutation path exists in the v1 contract) and cannot bypass approval gates.
- VIEWER: read-only (GET). Any mutation → 403 PERMISSION_DENIED.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import context
from app.core.errors import forbidden, invalid_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    role: str  # OWNER | AGENT | VIEWER

    @property
    def is_owner(self) -> bool:
        return self.role == "OWNER"

    @property
    def is_agent(self) -> bool:
        return self.role == "AGENT"

    @property
    def is_viewer(self) -> bool:
        return self.role == "VIEWER"

    @property
    def is_mutating_role(self) -> bool:
        return self.role in ("OWNER", "AGENT")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: str, role: str, secret: str, ttl_seconds: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> tuple[str, str, str | None]:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return str(payload["sub"]), str(payload["role"]), payload.get("jti")
    except jwt.ExpiredSignatureError as e:
        raise invalid_token() from e
    except jwt.InvalidTokenError as e:
        raise invalid_token() from e


async def get_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if credentials is None or not credentials.credentials:
        raise invalid_token("Missing bearer token")
    settings = request.app.state.settings
    user_id, role, jti = decode_access_token(credentials.credentials, settings.auth_jwt_secret)
    if role not in ("OWNER", "AGENT", "VIEWER"):
        raise invalid_token("Unknown role in token")
    # Load the user to enforce account state (active) and keep the role authoritative from the DB.
    db = request.app.state.db
    if jti:
        revoked = await db.tokens.find_by_hash(jti)
        if revoked is not None and revoked.get("revoked_at") is not None:
            raise invalid_token("Token has been revoked (session logged out)")
    user = await db.users.get_by_id(user_id)
    if user is None or not user.get("is_active", True):
        raise invalid_token("User is disabled")
    principal = Principal(user_id=user["id"], email=user["email"], role=user["role"])
    request.state.principal = principal
    context.user_id_var.set(principal.user_id)
    context.role_var.set(principal.role)
    context.actor_var.set(principal.user_id)
    return principal


def require_mutating(principal: Principal = Depends(get_principal)) -> Principal:
    """RBAC guard for every mutating operation (VIEWER is read-only)."""
    if not principal.is_mutating_role:
        raise forbidden(required="mutating role (OWNER or AGENT)")
    return principal


def require_owner(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_owner:
        raise forbidden(required="OWNER role")
    return principal


def set_action(action: str, target: str | None = None) -> None:
    context.action_var.set(action)
    if target:
        context.target_var.set(target)
