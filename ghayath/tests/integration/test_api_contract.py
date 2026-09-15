"""API contract tests: every implemented operation against the contract shapes,
status codes, error codes, and the unified envelope (validations 13, 16, 18)."""
from __future__ import annotations


import httpx

from tests.conftest import AGENT_EMAIL, OWNER_EMAIL, VIEWER_EMAIL, bearer, unique_key


def z_re(raw: str) -> str:
    """No datetime may leak in +00:00 form (contract: RFC3339 with Z)."""
    assert "+00:00" not in raw, f"non-Z datetime in response: {raw}"
    return raw


def data(resp: httpx.Response):
    body = resp.json()
    assert set(body) == {"success", "data", "request_id", "timestamp"}, f"envelope keys drifted: {set(body)}"
    assert body["success"] is True
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert body["timestamp"].endswith("Z")
    return body["data"]


def err(resp: httpx.Response) -> dict:
    body = resp.json()
    assert set(body) == {"success", "error", "request_id", "timestamp"}, f"error envelope keys drifted: {set(body)}"
    assert body["success"] is False
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] in (
        "AUTHENTICATION_FAILED", "INVALID_TOKEN", "PERMISSION_DENIED", "APPROVAL_REQUIRED",
        "VALIDATION_ERROR", "RESOURCE_NOT_FOUND", "CONFLICT", "RATE_LIMITED",
        "INTEGRATION_OFFLINE", "TOOL_UNAVAILABLE", "EXECUTION_FAILED", "VERIFICATION_FAILED",
        "TIMEOUT", "DEPENDENCY_FAILURE", "SECRET_ACCESS_DENIED")
    return body["error"]


# ── providers (mock transport boundary, P15) ───────────────────────────────
class MockProviders:
    """Deterministic provider at the HTTP transport boundary."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.calls = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.calls += 1
        path = request.url.path
        if path.endswith("/messages"):  # whatsapp
            return httpx.Response(200, json={"id": f"mock_wa_{self.calls}"})
        if path.endswith("/send"):  # email
            return httpx.Response(200, json={"id": f"mock_mail_{self.calls}"})
        if path.endswith("/actions/runs") or path.endswith("/actions/runs?per_page=1"):
            return httpx.Response(200, json=[])
        if "/issues?state=open" in path:
            return httpx.Response(200, json=[])
        if path.endswith("/issues"):
            return httpx.Response(201, json={"number": 42, "html_url": "https://github.com/x/1/issues/42"})
        return httpx.Response(200, json={})

    def transport(self):
        return httpx.MockTransport(self._handle)


def connect_mock(client, providers: MockProviders) -> None:
    """Configure adapters as connected (env-derived credentials) with a mock transport."""
    app = client.app
    for name, updates in (
        ("whatsapp", {"whatsapp_provider_token": "mock", "whatsapp_provider_base_url": "https://wa-mock"}),
        ("email", {"email_provider_token": "mock", "email_provider_base_url": "https://mail-mock"}),
        ("github", {"github_token": "mock"}),
    ):
        a = app.state.adapters[name]
        a._settings = a._settings.model_copy(update=updates)
        a._client = httpx.AsyncClient(transport=providers.transport())


async def approve(client, owner_token: str, approval_id: str) -> None:
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(owner_token),
                          json={"approved": True})
    assert r.status_code == 200, r.text


async def _new_project(client, t: str, name: str = "P") -> str:
    r = await client.post("/api/v1/projects", headers=bearer(t), json={"name": name, "priority": "P1"})
    assert r.status_code == 201, r.text
    return data(r)["id"]


# ── auth ───────────────────────────────────────────────────────────────────
async def test_login_success_envelope(client):
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "Sup3rSecret!x"})
    assert r.status_code == 200
    z_re(r.text)
    d = data(r)
    assert {"access_token", "refresh_token", "token_type", "expires_in", "user"} <= set(d)
    assert d["token_type"] == "Bearer"
    assert d["expires_in"] > 0
    assert d["user"]["role"] == "OWNER"


async def test_login_bad_password(client):
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "wrong-password"})
    assert r.status_code == 401
    assert err(r)["code"] in ("AUTHENTICATION_FAILED", "INVALID_TOKEN")


async def test_refresh_and_logout(client):
    r = await client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "Sup3rSecret!x"})
    d = data(r)
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": d["refresh_token"]})
    assert r2.status_code == 200
    new_token = data(r2)["access_token"]
    assert (await client.get("/api/v1/projects", headers=bearer(new_token))).status_code == 200
    r3 = await client.post("/api/v1/auth/logout", headers=bearer(new_token), json={})
    assert r3.status_code == 200
    assert (await client.get("/api/v1/projects", headers=bearer(new_token))).status_code == 401


async def test_no_or_bad_token(client):
    assert (await client.get("/api/v1/projects")).status_code == 401
    r = await client.get("/api/v1/projects", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401
    assert err(r)["code"] in ("INVALID_TOKEN", "AUTHENTICATION_FAILED")


# ── projects / tasks / memory ──────────────────────────────────────────────
async def test_projects_crud(client, login):
    t = await login(OWNER_EMAIL)
    pid = await _new_project(client, t, "MG Labs API")
    r = await client.get(f"/api/v1/projects/{pid}", headers=bearer(t))
    assert r.status_code == 200 and data(r)["name"] == "MG Labs API"
    r = await client.get("/api/v1/projects", headers=bearer(t))
    assert r.status_code == 200 and any(p["id"] == pid for p in data(r))
    r = await client.patch(f"/api/v1/projects/{pid}", headers=bearer(t), json={"description": "updated"})
    assert r.status_code == 200 and data(r)["description"] == "updated"
    r = await client.get("/api/v1/projects/prj_doesnotexist", headers=bearer(t))
    assert r.status_code == 404 and err(r)["code"] == "RESOURCE_NOT_FOUND"


async def test_tasks_flow(client, login):
    t = await login(OWNER_EMAIL)
    pid = await _new_project(client, t)
    r = await client.post("/api/v1/tasks", headers=bearer(t),
                          json={"project_id": pid, "title": "Ship it", "priority": "P1"})
    assert r.status_code == 201, r.text
    tid = data(r)["id"]
    r = await client.patch(f"/api/v1/tasks/{tid}", headers=bearer(t), json={"status": "IN_PROGRESS"})
    assert r.status_code == 200
    r = await client.post(f"/api/v1/tasks/{tid}/complete",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"verification": {"required": False, "status": "NOT_REQUIRED"}})
    assert r.status_code == 200, r.text
    d = data(r)
    assert d["status"] in ("DONE", "IN_REVIEW")
    assert d["verification_status"] in ("NOT_REQUIRED", "PENDING")


async def test_task_complete_requires_idempotency_key(client, login):
    t = await login(OWNER_EMAIL)
    pid = await _new_project(client, t)
    tid = data(await client.post("/api/v1/tasks", headers=bearer(t),
                                 json={"project_id": pid, "title": "T", "priority": "P2"}))["id"]
    r = await client.post(f"/api/v1/tasks/{tid}/complete", headers=bearer(t),
                          json={"verification": {"required": False, "status": "NOT_REQUIRED"}})
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


async def test_task_verification_gate(client, login):
    """A completion claimed with required verification must NOT end DONE — it waits review."""
    t = await login(OWNER_EMAIL)
    pid = await _new_project(client, t)
    tid = data(await client.post("/api/v1/tasks", headers=bearer(t),
                                 json={"project_id": pid, "title": "V", "priority": "P1"}))["id"]
    r = await client.post(f"/api/v1/tasks/{tid}/complete",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"verification": {"required": True, "status": "PENDING"}})
    assert r.status_code == 200, r.text
    d = data(r)
    assert d["status"] == "IN_REVIEW" and d["verification_status"] == "PENDING"
    row = await client.app.state.db.fetchone("SELECT status FROM tasks WHERE id = %s", tid)
    assert row["status"] == "IN_REVIEW"


async def test_memory_crud(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/memory", headers=bearer(t),
                          json={"type": "PREFERENCE", "key": "client_style", "value": {"style": "direct"}})
    assert r.status_code == 201, r.text
    mid = data(r)["id"]
    r = await client.get("/api/v1/memory", headers=bearer(t))
    assert r.status_code == 200 and any(m["id"] == mid for m in data(r))
    r = await client.delete(f"/api/v1/memory/{mid}", headers=bearer(t))
    assert r.status_code == 200 and data(r)["deleted"] is True


# ── whatsapp ───────────────────────────────────────────────────────────────
async def test_whatsapp_status_disconnected(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.get("/api/v1/integrations/whatsapp/status", headers=bearer(t))
    assert r.status_code == 200
    d = data(r)
    assert d["connected"] is False and d["provider"] == "whatsapp"


async def test_whatsapp_send_offline(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/whatsapp/messages/send",
                          json={"recipient": "+963930123456", "message": "hi"},
                          headers={**bearer(t), "Idempotency-Key": unique_key()})
    # OWNER is allowed by role; provider unconnected → explicit INTEGRATION_OFFLINE.
    assert r.status_code == 503, r.text
    assert err(r)["code"] == "INTEGRATION_OFFLINE"


async def test_whatsapp_send_bad_recipient(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/whatsapp/messages/send",
                          json={"recipient": "0930123456", "message": "hi"},
                          headers={**bearer(t), "Idempotency-Key": unique_key()})
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


async def test_whatsapp_agent_approval_then_send(client, login):
    """AGENT without the SEND bit: 409 + approval_id → OWNER approves → identical
    resubmit (contract request has no approval field) executes exactly once."""
    a = await login(AGENT_EMAIL)
    o = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    key = unique_key()
    payload = {"recipient": "+963930123456", "message": "Demo booked for Friday"}
    r = await client.post("/api/v1/whatsapp/messages/send",
                          json=payload, headers={**bearer(a), "Idempotency-Key": key})
    assert r.status_code == 409, r.text
    e = err(r)
    assert e["code"] == "APPROVAL_REQUIRED" and "approval_id" in e["details"]
    # nothing must have gone to the provider before approval
    assert len(providers.requests) == 0
    await approve(client, o, e["details"]["approval_id"])
    r = await client.post("/api/v1/whatsapp/messages/send",
                          json=payload, headers={**bearer(a), "Idempotency-Key": key})
    assert r.status_code == 200, r.text
    d = data(r)
    assert d["status"] == "SENT" and d["message_id"].startswith("wmsg_")
    assert len(providers.requests) == 1
    row = await client.app.state.db.fetchone(
        "SELECT * FROM whatsapp_messages WHERE idempotency_key = %s", key)
    assert row is not None and row["status"] == "SENT"


async def test_whatsapp_missing_idempotency_key(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/whatsapp/messages/send", headers=bearer(t),
                          json={"recipient": "+963930123456", "message": "hi"})
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


# ── email ──────────────────────────────────────────────────────────────────
async def test_email_high_impact_approval_flow(client, login):
    """High-impact content forces approval even for OWNER (locked decision)."""
    t = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    r = await client.post("/api/v1/email/drafts", headers=bearer(t),
                          json={"to": "client@example.com", "subject": "Invoice #12", "body": "Please find attached"})
    assert r.status_code == 201, r.text
    draft_id = data(r)["id"]
    r = await client.post("/api/v1/email/send",
                          json={"draft_id": draft_id},
                          headers={**bearer(t), "Idempotency-Key": unique_key()})
    assert r.status_code == 409, r.text
    assert err(r)["code"] == "APPROVAL_REQUIRED"
    approval_id = err(r)["details"]["approval_id"]
    await approve(client, t, approval_id)
    r = await client.post("/api/v1/email/send",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"draft_id": draft_id, "approval_id": approval_id})
    assert r.status_code == 200, r.text
    d = data(r)
    assert d["status"] == "SENT" and d["draft_id"] == draft_id


async def test_email_send_offline(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/email/drafts", headers=bearer(t),
                          json={"to": "x@example.com", "subject": "Hello", "body": "World"})
    draft_id = data(r)["id"]
    r = await client.post("/api/v1/email/send",
                          json={"draft_id": draft_id},
                          headers={**bearer(t), "Idempotency-Key": unique_key()})
    assert r.status_code == 503 and err(r)["code"] == "INTEGRATION_OFFLINE"


async def test_email_list_and_get(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.get("/api/v1/email/messages", headers=bearer(t))
    assert r.status_code == 200
    assert (await client.get("/api/v1/email/messages/msg_nope", headers=bearer(t))).status_code == 404


# ── github ─────────────────────────────────────────────────────────────────
async def _seed_repo(client, full_name: str = "mody101220/MG-LABS", project_id: str | None = None) -> str:
    import psycopg

    owner, name = full_name.split("/", 1)
    dsn = client.app.state.test_dsn
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO github_repositories (id, full_name, owner, name, default_branch, project_id) "
            "VALUES ('repo_mock1', %s, %s, %s, 'main', %s)", (full_name, owner, name, project_id))
    return "repo_mock1"


async def test_github_repos_status_issue_offline(client, login):
    t = await login(OWNER_EMAIL)
    await _seed_repo(client)
    r = await client.get("/api/v1/integrations/github/repositories", headers=bearer(t))
    assert r.status_code == 200 and any(x["id"] == "repo_mock1" for x in data(r))
    r = await client.get("/api/v1/github/repos/repo_mock1/status", headers=bearer(t))
    assert r.status_code == 200
    assert data(r)["build"] in ("PASSING", "FAILING", "UNKNOWN")
    r = await client.post("/api/v1/github/issues", headers=bearer(t),
                          json={"repository": "mody101220/MG-LABS", "title": "Bug"})
    assert r.status_code == 503 and err(r)["code"] == "INTEGRATION_OFFLINE"


async def test_github_issue_with_mock(client, login):
    t = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    await _seed_repo(client)
    r = await client.post("/api/v1/github/issues", headers=bearer(t),
                          json={"repository": "mody101220/MG-LABS", "title": "Bug", "labels": ["bug"]})
    assert r.status_code == 201, r.text
    d = data(r)
    assert d["number"] == 42 and d["status"] == "OPEN"
    assert any(req.url.path.endswith("/issues") for req in providers.requests)


# ── monitoring / automations / approvals / permissions ─────────────────────
async def test_monitoring_check(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/monitoring/check", headers=bearer(t),
                          json={"target_type": "SERVICE", "target_id": "svc-api", "checks": ["DATABASE"]})
    assert r.status_code == 200, r.text
    d = data(r)
    assert d["status"] in ("HEALTHY", "DEGRADED", "CRITICAL", "UNKNOWN")
    assert any(res["check"] == "DATABASE" and res["status"] == "PASSING" for res in d["results"])
    r = await client.post("/api/v1/monitoring/check", headers=bearer(t),
                          json={"target_type": "PROJECT", "target_id": "prj_missing"})
    assert r.status_code == 404 and err(r)["code"] == "RESOURCE_NOT_FOUND"


async def test_automations_create_update(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/automations", headers=bearer(t), json={
        "name": "Daily check",
        "trigger": {"type": "SCHEDULE", "cron": "0 9 * * *"},
        "action": {"type": "RUN_CHECK", "params": {"target_type": "SERVICE", "target": "svc-api"}},
    })
    assert r.status_code == 201, r.text
    aid = data(r)["id"]
    r = await client.patch(f"/api/v1/automations/{aid}", headers=bearer(t), json={"enabled": False})
    assert r.status_code == 200 and data(r)["enabled"] is False
    assert (await client.patch("/api/v1/automations/aut_nope", headers=bearer(t),
                               json={"enabled": True})).status_code == 404


async def test_approvals_lifecycle(client, login):
    a = await login(AGENT_EMAIL)
    o = await login(OWNER_EMAIL)
    # create an approval via the agent execution gate
    r = await client.post("/api/v1/agent/command", headers=bearer(a), json={"command": "hello there"})
    assert r.status_code == 201
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd})
    assert r.status_code == 409
    approval_id = err(r)["details"]["approval_id"]
    # agent cannot approve (OWNER-only)
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(a), json={"approved": True})
    assert r.status_code == 403
    # owner approves
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(o), json={"approved": True})
    assert r.status_code == 200
    d = data(r)
    assert d["status"] == "APPROVED" and d["approval_id"] == approval_id and d["decided_at"].endswith("Z")
    # re-approving → conflict
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(o), json={"approved": True})
    assert r.status_code == 409 and err(r)["code"] == "CONFLICT"
    # list shows APPROVED (listApprovals defaults to the PENDING filter)
    r = await client.get("/api/v1/approvals?status=APPROVED", headers=bearer(o))
    assert r.status_code == 200 and any(x["id"] == approval_id and x["status"] == "APPROVED" for x in data(r))
    # resubmit with the grant: execution now reaches the per-resource permission stage,
    # where the AGENT's missing bits (all false) deny it — honestly, with 403.
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd, "approval_id": approval_id})
    assert r.status_code == 403 and err(r)["code"] == "PERMISSION_DENIED"


async def test_approvals_reject(client, login):
    a = await login(AGENT_EMAIL)
    o = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(a), json={"command": "hello there"})
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd})
    approval_id = err(r)["details"]["approval_id"]
    r = await client.post(f"/api/v1/approvals/{approval_id}/reject", headers=bearer(o),
                          json={"reason": "not now"})
    assert r.status_code == 200 and data(r)["status"] == "REJECTED"


async def test_permissions_endpoints(client, login):
    t = await login(OWNER_EMAIL)
    a = await login(AGENT_EMAIL)
    r = await client.get("/api/v1/permissions", headers=bearer(t))
    assert r.status_code == 200
    assert set(data(r)) == {"WHATSAPP", "EMAIL", "GITHUB"}
    r = await client.post("/api/v1/permissions/check", headers=bearer(t),
                          json={"resource": "WHATSAPP", "action": "SEND"})
    assert r.status_code == 200 and data(r)["allowed"] is True
    r = await client.post("/api/v1/permissions/check", headers=bearer(a),
                          json={"resource": "WHATSAPP", "action": "SEND"})
    d = data(r)
    assert d["allowed"] is False and d["requires_approval"] is True


async def test_no_permission_grant_path(client, login):
    a = await login(AGENT_EMAIL)
    for method, path in (("put", "/api/v1/permissions"), ("patch", "/api/v1/permissions"),
                          ("put", "/api/v1/integrations/whatsapp/permissions")):
        r = await client.request(method, path, headers=bearer(a), json={})
        assert r.status_code in (404, 405), f"{method} {path} must not exist"


# ── notifications / audit / incidents / brief / system ─────────────────────
async def test_notifications_create(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/notifications", headers=bearer(t),
                          json={"severity": "LOW", "title": "Deploy done", "message": "All green"})
    assert r.status_code == 201, r.text
    d = data(r)
    assert d["notification_id"].startswith("notf_")
    assert d["routed_channels"] == ["DASHBOARD"]
    assert d["status"] in ("QUEUED", "SENT", "FAILED", "DISMISSED")


async def test_audit_list(client, login):
    t = await login(OWNER_EMAIL)
    await _new_project(client, t, "Audit me")
    r = await client.get("/api/v1/audit", headers=bearer(t))
    assert r.status_code == 200
    items = data(r)
    assert items, "audit must record important actions"
    assert {"timestamp", "actor", "action", "target_type", "result"} <= set(items[0])
    assert any(x["action"] == "PROJECT_CREATED" for x in items)


async def test_incidents_create_idempotent(client, login):
    t = await login(OWNER_EMAIL)
    key = unique_key()
    body = {"severity": "HIGH", "system": "api", "issue": "5xx spike", "impact": "users affected"}
    r = await client.post("/api/v1/incidents", json=body,
                          headers={**bearer(t), "Idempotency-Key": key})
    assert r.status_code == 201, r.text
    iid = data(r)["incident_id"]
    # NOTE: the v1 contract has no GET /incidents/{id} — a 404 is the correct behavior.
    r = await client.post("/api/v1/incidents", json=body, headers=bearer(t))
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"
    # same key + same body → original result replayed, no duplicate incident
    r = await client.post("/api/v1/incidents", json=body,
                          headers={**bearer(t), "Idempotency-Key": key})
    assert r.status_code == 201 and data(r)["incident_id"] == iid
    count = await client.app.state.db.fetchone("SELECT count(*)::int AS n FROM incidents WHERE id = %s", iid)
    assert count["n"] == 1


async def test_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    d = data(r)
    # no redis configured → honestly degraded, database healthy
    assert d["status"] == "degraded"
    assert d["services"]["database"] == "healthy"
    assert d["services"]["redis"] == "down"


async def test_system_status(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.get("/api/v1/system/status", headers=bearer(t))
    assert r.status_code == 200
    d = data(r)
    assert d["system"] in ("OPERATIONAL", "DEGRADED", "MAINTENANCE", "DOWN")
    assert d["integrations"]["whatsapp"] in ("CONNECTED", "DISCONNECTED", "DEGRADED")
    assert d["security"] in ("PROTECTED", "DEGRADED")


async def test_daily_brief(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.get("/api/v1/brief/daily", headers=bearer(t))
    assert r.status_code == 200, r.text
    d = data(r)
    for field in ("date", "critical", "high", "tasks", "emails", "whatsapp",
                  "projects", "deadlines", "actions_taken", "next_actions"):
        assert field in d, f"brief missing {field}"


# ── RBAC ───────────────────────────────────────────────────────────────────
async def test_viewer_read_only(client, login):
    v = await login(VIEWER_EMAIL)
    assert (await client.get("/api/v1/projects", headers=bearer(v))).status_code == 200
    r = await client.post("/api/v1/projects", headers=bearer(v), json={"name": "Nope", "priority": "P2"})
    assert r.status_code == 403 and err(r)["code"] == "PERMISSION_DENIED"
    r = await client.post("/api/v1/incidents",
                          json={"severity": "LOW", "system": "s", "issue": "i", "impact": "none"},
                          headers={**bearer(v), "Idempotency-Key": unique_key()})
    assert r.status_code == 403
    # owner-only surface
    assert (await client.get("/api/v1/approvals", headers=bearer(v))).status_code == 403


async def test_agent_cannot_approve(client, login):
    a = await login(AGENT_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(a), json={"command": "hello there"})
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd})
    approval_id = err(r)["details"]["approval_id"]
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(a), json={"approved": True})
    assert r.status_code == 403


# ── agent core ─────────────────────────────────────────────────────────────
async def test_agent_command_planning(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t),
                          json={"command": "check the project status for prj_demo1"})
    assert r.status_code == 201, r.text
    d = data(r)
    assert d["command_id"].startswith("cmd_")
    assert d["intent"] == "PROJECT_MONITORING"
    assert d["status"] == "PLANNED"
    assert [s["task"] for s in d["plan"]] == ["GET_PROJECT_STATUS", "CHECK_OPEN_ISSUES", "CHECK_DEPLOYMENT"]


async def test_agent_execute_completed(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t), json={"command": "hello there"})
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd})
    assert r.status_code == 202, r.text
    d = data(r)
    assert d["status"] == "COMPLETED" and d["execution_id"].startswith("exec_")
    ver = await client.app.state.db.fetchone(
        "SELECT v.* FROM verifications v JOIN executions e ON e.verification_id = v.id WHERE e.command_id = %s", cmd)
    assert ver is not None and ver["status"] == "PASSED"


async def test_agent_execute_tool_unavailable_never_fakes(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t),
                          json={"command": "check the project status for prj_demo1"})
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"command_id": cmd})
    assert r.status_code == 202, r.text
    assert data(r)["status"] == "FAILED"  # honest outcome, never a fake success
    row = await client.app.state.db.fetchone("SELECT * FROM executions WHERE command_id = %s", cmd)
    assert row["status"] == "FAILED"
    assert row["error_code"] in ("TOOL_UNAVAILABLE", "RESOURCE_NOT_FOUND", "EXECUTION_FAILED")


async def test_agent_execute_requires_idempotency_key(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t), json={"command": "hello there"})
    cmd = data(r)["command_id"]
    r = await client.post("/api/v1/agent/execute", headers=bearer(t), json={"command_id": cmd})
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


async def test_agent_execute_unknown_command(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(t), "Idempotency-Key": unique_key()},
                          json={"command_id": "cmd_nope"})
    assert r.status_code == 404 and err(r)["code"] == "RESOURCE_NOT_FOUND"


async def test_agent_execute_twice_conflict(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/agent/command", headers=bearer(t), json={"command": "hello there"})
    cmd = data(r)["command_id"]
    r1 = await client.post("/api/v1/agent/execute",
                           headers={**bearer(t), "Idempotency-Key": unique_key()},
                           json={"command_id": cmd})
    assert r1.status_code == 202
    r2 = await client.post("/api/v1/agent/execute",
                           headers={**bearer(t), "Idempotency-Key": unique_key()},
                           json={"command_id": cmd})
    assert r2.status_code == 409 and err(r2)["code"] == "CONFLICT"


# ── envelope discipline ────────────────────────────────────────────────────
async def test_error_never_leaks_stack(client):
    r = await client.get("/api/v1/projects/prj_missing", headers={"Authorization": "Bearer x"})
    body = r.text
    assert "Traceback" not in body and "File \"" not in body
    assert err(r)["code"] in ("INVALID_TOKEN", "AUTHENTICATION_FAILED")


async def test_validation_error_envelope(client, login):
    t = await login(OWNER_EMAIL)
    r = await client.post("/api/v1/projects", headers=bearer(t),
                          json={"name": "X", "priority": "P9"})
    assert r.status_code == 400, r.text
    assert err(r)["code"] == "VALIDATION_ERROR"
    r = await client.post("/api/v1/projects", headers=bearer(t), json={"name": ""})
    assert r.status_code == 400, r.text  # minLength 1
    assert "Traceback" not in r.text
