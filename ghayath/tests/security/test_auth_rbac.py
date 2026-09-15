"""Validation 8-9: authentication + 3-role RBAC."""
from __future__ import annotations

import jwt

from tests.conftest import AGENT_EMAIL, OWNER_EMAIL, VIEWER_EMAIL, bearer, unique_key

JWT_SECRET = "test-jwt-secret-not-for-production"


async def test_unknown_user_rejected(client):
    r = await client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "whatever"})
    assert r.status_code == 401
    body = r.text
    assert "Traceback" not in body


async def test_token_tampered_signature(client):
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "Sup3rSecret!x"})
    tok = r.json()["data"]["access_token"]
    header, payload, _sig = tok.split(".")
    forged = header + "." + payload + ".AAAA"
    r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] in ("INVALID_TOKEN", "AUTHENTICATION_FAILED")


async def test_token_from_wrong_secret_rejected(client):
    import time

    tok = jwt.encode(
        {"sub": "usr_owner", "role": "OWNER", "exp": int(time.time()) + 3600},
        "some-other-secret", algorithm="HS256",
    )
    r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_expired_token_rejected(client):
    import time

    tok = jwt.encode(
        {"sub": "usr_owner", "role": "OWNER", "exp": int(time.time()) - 10},
        JWT_SECRET, algorithm="HS256",
    )
    r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_expired_refresh_rejected(client):
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "Sup3rSecret!x"})
    rtok = r.json()["data"]["refresh_token"]
    # refresh tokens are opaque (hashed at rest); force the row past its expiry in the DB
    import hashlib
    from datetime import datetime, timezone

    h = hashlib.sha256(rtok.encode()).hexdigest()
    await client.app.state.db.execute(
        "UPDATE auth_tokens SET expires_at = %s WHERE token_hash = %s",
        (datetime.now(timezone.utc), h))
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": rtok})
    assert r.status_code == 401


async def test_viewer_cannot_mutate_any_endpoint(client, login):
    v = await login(VIEWER_EMAIL)
    mutations = [
        ("post", "/api/v1/projects", {"name": "X", "priority": "P2"}),
        ("post", "/api/v1/tasks", {"project_id": "prj_x", "title": "t", "priority": "P2"}),
        ("post", "/api/v1/agent/command", {"command": "hello"}),
        ("post", "/api/v1/memory", {"type": "PREFERENCE", "key": "k", "value": {}}),
        ("delete", "/api/v1/memory/mem_x", None),
        ("post", "/api/v1/notifications", {"severity": "LOW", "title": "t", "message": "m"}),
        ("post", "/api/v1/incidents", {"severity": "LOW", "system": "s", "issue": "i", "impact": "n"}),
        ("post", "/api/v1/automations", {"name": "a", "trigger": {"type": "MANUAL"}, "action": {"type": "CREATE_TASK"}}),
    ]
    for method, path, body in mutations:
        headers = bearer(v)
        if method == "post" and path in ("/api/v1/incidents",):
            headers = {**headers, "Idempotency-Key": unique_key()}
        r = await client.request(method, path, headers=headers, json=body)
        assert r.status_code == 403, f"{method} {path}: expected 403, got {r.status_code}"
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_viewer_can_read(client, login):
    v = await login(VIEWER_EMAIL)
    for path in ("/api/v1/projects", "/api/v1/tasks", "/api/v1/memory", "/api/v1/audit",
                 "/api/v1/permissions", "/api/v1/health"):
        r = await client.get(path, headers=bearer(v))
        assert r.status_code == 200, f"GET {path} for VIEWER: {r.status_code}"


async def test_owner_only_surfaces(client, login):
    a = await login(AGENT_EMAIL)
    v = await login(VIEWER_EMAIL)
    for tok in (a, v):
        assert (await client.get("/api/v1/approvals", headers=bearer(tok))).status_code == 403
        assert (await client.get("/api/v1/system/status", headers=bearer(v))).status_code in (200, 403)


async def test_agent_cannot_grant_itself_permissions(client, login):
    a = await login(AGENT_EMAIL)
    r = await client.post("/api/v1/permissions/check", headers=bearer(a),
                          json={"resource": "WHATSAPP", "action": "SEND"})
    assert r.status_code == 200 and r.json()["data"]["allowed"] is False
    # the only surfaces that exist under /permissions are read-only
    r = await client.request("put", "/api/v1/permissions", headers=bearer(a), json={})
    assert r.status_code in (404, 405)


async def test_agent_approval_gate_cannot_be_bypassed(client, login):
    """AGENT execute with a fabricated approval id must not run."""
    a = await login(AGENT_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(a), json={"command": "hello there"})
    cmd = r.json()["data"]["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd, "approval_id": "appr_fabricated123"})
    # a fabricated approval id is rejected (404 not found / 409 mismatch) — no execution
    assert r.status_code in (404, 409), r.text
    row = await client.app.state.db.fetchone("SELECT count(*)::int AS n FROM executions WHERE command_id = %s", cmd)
    assert row["n"] == 0


async def test_login_rate_limit_enforced(client):
    """Contract §28: /auth/* = 5 requests/minute."""
    codes = []
    for _ in range(6):
        r = await client.post("/api/v1/auth/login",
                              json={"email": OWNER_EMAIL, "password": "wrong-password"})
        codes.append(r.status_code)
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    e = r.json()["error"]
    assert e["code"] == "RATE_LIMITED"
    assert "X-RateLimit-Reset" in r.headers
