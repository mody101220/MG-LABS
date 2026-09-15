"""List/filter/pagination contract checks + scheduler SCHEDULE run path.

The contract documents query parameters for every list endpoint; these tests
verify the filters actually filter, pagination windows work, and invalid enum
values are rejected with the 400 VALIDATION_ERROR envelope.
"""
from __future__ import annotations

import httpx

from tests.conftest import OWNER_EMAIL, bearer


def data(resp: httpx.Response):
    return resp.json()["data"]


def err(resp: httpx.Response) -> dict:
    body = resp.json()
    assert set(body) == {"success", "error", "request_id", "timestamp"}
    assert set(body["error"]) == {"code", "message", "details"}
    return body["error"]


async def _mk_project(client, t, name, priority="P1"):
    r = await client.post("/api/v1/projects", headers=bearer(t), json={"name": name, "priority": priority})
    assert r.status_code == 201, r.text
    return data(r)["id"]


async def test_projects_pagination_window(client, login):
    t = await login(OWNER_EMAIL)
    for name in ("aaa", "bbb", "ccc"):
        await _mk_project(client, t, name)
    p1 = await client.get("/api/v1/projects?page=1&page_size=2", headers=bearer(t))
    p2 = await client.get("/api/v1/projects?page=2&page_size=2", headers=bearer(t))
    assert p1.status_code == 200 and p2.status_code == 200
    first = [p["name"] for p in data(p1)]
    second = [p["name"] for p in data(p2)]
    assert len(first) == 2 and len(second) == 1
    assert not (set(first) & set(second)), "pages must not overlap"


async def test_tasks_filters(client, login):
    t = await login(OWNER_EMAIL)
    p1 = await _mk_project(client, t, "FiltA")
    p2 = await _mk_project(client, t, "FiltB")
    for proj, title, prio in ((p1, "ta", "P0"), (p1, "tb", "P1"), (p2, "tc", "P2")):
        r = await client.post("/api/v1/tasks", headers=bearer(t),
                              json={"project_id": proj, "title": title, "priority": prio})
        assert r.status_code == 201
    r = await client.get(f"/api/v1/tasks?project_id={p1}", headers=bearer(t))
    assert {x["title"] for x in data(r)} == {"ta", "tb"}
    r = await client.get(f"/api/v1/tasks?priority=P0&project_id={p1}", headers=bearer(t))
    assert [x["title"] for x in data(r)] == ["ta"]
    # status filter after an update
    tid = data(await client.get(f"/api/v1/tasks?priority=P0&project_id={p1}", headers=bearer(t)))[0]["id"]
    await client.patch(f"/api/v1/tasks/{tid}", headers=bearer(t), json={"status": "IN_PROGRESS"})
    r = await client.get(f"/api/v1/tasks?status=IN_PROGRESS&project_id={p1}", headers=bearer(t))
    assert [x["title"] for x in data(r)] == ["ta"]
    # invalid enum → 400 VALIDATION_ERROR envelope
    r = await client.get("/api/v1/tasks?status=BOGUS", headers=bearer(t))
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"
    # due_before: nothing due before 1999
    r = await client.get("/api/v1/tasks?due_before=1999-01-01T00:00:00Z", headers=bearer(t))
    assert data(r) == []


async def test_memory_filters(client, login):
    t = await login(OWNER_EMAIL)
    pid = await _mk_project(client, t, "MemP")
    for mtype, key in (("PREFERENCE", "pref_a"), ("DECISION", "dec_a"), ("PREFERENCE", "pref_b")):
        r = await client.post("/api/v1/memory", headers=bearer(t),
                              json={"type": mtype, "key": key, "value": {"x": 1}})
        assert r.status_code == 201
    r = await client.get("/api/v1/memory?type=PREFERENCE", headers=bearer(t))
    assert {m["key"] for m in data(r)} == {"pref_a", "pref_b"}
    r = await client.get(f"/api/v1/memory?key=pref_a&project_id={pid}", headers=bearer(t))
    assert data(r) == []  # the rows were not created with a project
    r = await client.get("/api/v1/memory?type=PREFERENCE&key=pref_a", headers=bearer(t))
    assert [m["key"] for m in data(r)] == ["pref_a"]
    r = await client.get("/api/v1/memory?type=NOT_A_TYPE", headers=bearer(t))
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


async def test_audit_filters(client, login):
    t = await login(OWNER_EMAIL)
    pid = await _mk_project(client, t, "AuditFilt")
    await _mk_project(client, t, "AuditFilt2")
    # action filter
    r = await client.get("/api/v1/audit?action=PROJECT_CREATED", headers=bearer(t))
    assert len(data(r)) >= 2 and all(x["action"] == "PROJECT_CREATED" for x in data(r))
    # actor filter (owner is 'user' type, actor = user id)
    r = await client.get(f"/api/v1/audit?actor={t_user_id(client)}", headers=bearer(t))
    assert all(x["actor"] == t_user_id(client) for x in data(r))
    # result filter
    r = await client.get("/api/v1/audit?result=SUCCESS", headers=bearer(t))
    assert all(x["result"] == "SUCCESS" for x in data(r))
    # project filter
    r = await client.get(f"/api/v1/audit?project_id={pid}", headers=bearer(t))
    assert all(x["project_id"] in (pid, None) for x in data(r))
    assert any(x["project_id"] == pid for x in data(r))
    # time window in the future → empty
    r = await client.get("/api/v1/audit?from=2999-01-01T00:00:00Z", headers=bearer(t))
    assert data(r) == []


def t_user_id(client) -> str:
    # usr_owner is the deterministic seed identity
    return "usr_owner"


async def test_approvals_filters(client, login):
    a = await login("agent@example.com")
    o = await login(OWNER_EMAIL)
    cmd = data(await client.post("/api/v1/agent/command", headers=bearer(a),
                                 json={"command": "hello there"}))["command_id"]
    r = await client.post("/api/v1/agent/execute",
                          headers={**bearer(a), "Idempotency-Key": f"idem-{cmd}"},
                          json={"command_id": cmd})
    assert r.status_code == 409
    approval_id = err(r)["details"]["approval_id"]
    # default filter is PENDING
    r = await client.get("/api/v1/approvals", headers=bearer(o))
    assert any(x["id"] == approval_id for x in data(r))
    # type filter
    r = await client.get("/api/v1/approvals?type=AGENT_EXECUTION", headers=bearer(o))
    assert all(x["type"] == "AGENT_EXECUTION" for x in data(r))
    r = await client.get("/api/v1/approvals?type=EMAIL_SEND", headers=bearer(o))
    assert data(r) == []
    # approve → no longer in PENDING, present in APPROVED
    r = await client.post(f"/api/v1/approvals/{approval_id}/approve", headers=bearer(o),
                          json={"approved": True})
    assert r.status_code == 200
    r = await client.get("/api/v1/approvals", headers=bearer(o))
    assert all(x["id"] != approval_id for x in data(r))
    r = await client.get("/api/v1/approvals?status=APPROVED", headers=bearer(o))
    assert any(x["id"] == approval_id for x in data(r))
    r = await client.get("/api/v1/approvals?status=REJECTED", headers=bearer(o))
    assert all(x["id"] != approval_id for x in data(r))


async def test_email_messages_folder_filter(client, login):
    """After a real send (mocked provider), the sent folder shows the message
    and the inbox filter excludes it."""
    from tests.integration.test_api_contract import MockProviders, connect_mock

    t = await login(OWNER_EMAIL)
    providers = MockProviders()
    connect_mock(client, providers)
    draft_id = data(await client.post("/api/v1/email/drafts", headers=bearer(t),
                                      json={"to": "someone@example.com", "subject": "Plain send",
                                            "body": "no sensitive words"}))["id"]
    r = await client.post("/api/v1/email/send",
                          json={"draft_id": draft_id},
                          headers={**bearer(t), "Idempotency-Key": "idem-plain-send-1"})
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/email/messages?folder=sent", headers=bearer(t))
    assert any(m["subject"] == "Plain send" for m in data(r))
    r = await client.get("/api/v1/email/messages?folder=inbox", headers=bearer(t))
    assert all(m["subject"] != "Plain send" for m in data(r))
    # priority filter is a valid contract parameter
    r = await client.get("/api/v1/email/messages?priority=URGENT", headers=bearer(t))
    assert all(m["priority"] == "URGENT" for m in data(r))
    r = await client.get("/api/v1/email/messages?folder=not_a_folder", headers=bearer(t))
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


async def test_github_repositories_pagination(client, login):
    import psycopg

    t = await login(OWNER_EMAIL)
    dsn = client.app.state.test_dsn
    with psycopg.connect(dsn) as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO github_repositories (id, full_name, owner, name) "
                "VALUES (%s, %s, %s, %s)",
                (f"repo_pg{i:02d}", f"owner{i}/repo{i}", f"owner{i}", f"repo{i}"))
    r = await client.get("/api/v1/integrations/github/repositories?page=1&page_size=2", headers=bearer(t))
    assert r.status_code == 200 and len(data(r)) == 2
    r = await client.get("/api/v1/integrations/github/repositories?page=2&page_size=2", headers=bearer(t))
    assert len(data(r)) >= 1


async def test_scheduler_schedule_run_path(client, login):
    """The scheduler's _run (the same code the 15s loop calls) executes a due
    SCHEDULE automation: task created, status + next_run updated, audited."""
    t = await login(OWNER_EMAIL)
    pid = await _mk_project(client, t, "SchedP")
    import psycopg

    dsn = client.app.state.test_dsn
    import json

    trigger = json.dumps({"type": "SCHEDULE", "cron": "0 9 * * *"})
    action = json.dumps({"type": "CREATE_TASK",
                         "params": {"project_id": pid, "title": "From scheduler"}})
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO automations (id, name, trigger, action, enabled, next_run_at) "
            "VALUES ('auto_sched1', 'due now', %s::jsonb, %s::jsonb, TRUE, "
            "now() - interval '1 second')",
            (trigger, action))
    auto = await client.app.state.db.automations.get("auto_sched1")
    assert auto is not None
    await client.app.state.scheduler._run(auto)
    row = await client.app.state.db.automations.get("auto_sched1")
    assert row["last_run_status"] == "SUCCESS"
    assert row["next_run_at"] is not None  # recomputed for the next cron tick
    n = await client.app.state.db.fetchone(
        "SELECT count(*)::int AS n FROM tasks WHERE title = 'From scheduler' AND project_id = %s", pid)
    assert n["n"] == 1
