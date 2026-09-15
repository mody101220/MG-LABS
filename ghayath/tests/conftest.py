"""Shared fixtures: real embedded PostgreSQL 16 (pgserver), migrated schema,
fresh app per test, seeded OWNER/AGENT/VIEWER users, httpx ASGI client."""
from __future__ import annotations

import pathlib
import secrets

import httpx
import pytest
import psycopg
from pgserver import get_server

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app

PG_DATA = pathlib.Path("/tmp/ghayath-pg-test")
OWNER_EMAIL = "owner@example.com"
AGENT_EMAIL = "agent@example.com"
VIEWER_EMAIL = "viewer@example.com"
PASSWORD = "Sup3rSecret!x"
WEBHOOK_SECRET = "test-webhook-secret"

ALL_TABLES = [
    "users", "auth_tokens", "conversations", "conversation_messages",
    "projects", "project_integrations", "tasks", "verifications",
    "agent_commands", "executions", "approvals", "automations",
    "notifications", "audit_logs", "incidents", "integrations",
    "whatsapp_messages", "email_messages", "email_drafts",
    "github_repositories", "repo_status_snapshots", "github_issues",
    "memory", "events", "idempotency_keys",
]


@pytest.fixture(scope="session")
def pg():
    """Real PostgreSQL 16.2 server for the whole test session; DDL installed once."""
    srv = get_server(PG_DATA, cleanup_mode="stop")
    srv.ensure_pgdata_inited()
    srv.ensure_postgres_running()
    dsn = srv.get_uri()
    conn = psycopg.connect(dsn, autocommit=True)
    installed = conn.execute("SELECT to_regclass('public.users') IS NOT NULL").fetchone()[0]
    if not installed:
        from app.db.migrate import load_migration_sql

        conn.execute(load_migration_sql())
    conn.close()
    yield dsn
    srv.cleanup()


def _reset(dsn: str) -> None:
    """Truncate all tables and re-seed deterministic users + integration rows."""
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f"TRUNCATE {', '.join(ALL_TABLES)} CASCADE")
        conn.execute(
            """INSERT INTO integrations (id, provider, permissions) VALUES
               ('whatsapp','whatsapp','{"READ": false, "REPLY": false, "SEND": false, "MEDIA": false}'),
               ('email','email','{"READ": false, "DRAFT": false, "SEND": false, "DELETE": false}'),
               ('github','github','{"READ": false, "ISSUES": false, "PULL_REQUEST": false, "COMMIT": false, "MERGE": false}')""")
        for uid, email, role in (("usr_owner", OWNER_EMAIL, "OWNER"),
                                  ("usr_agent", AGENT_EMAIL, "AGENT"),
                                  ("usr_viewer", VIEWER_EMAIL, "VIEWER")):
            conn.execute(
                "INSERT INTO users (id, email, password_hash, role, name) VALUES (%s,%s,%s,%s,%s)",
                (uid, email, hash_password(PASSWORD), role, role.title()),
            )
    finally:
        conn.close()


@pytest.fixture
def db_reset(pg):
    _reset(pg)
    yield pg


def make_settings(dsn: str) -> Settings:
    return Settings(
        database_url=dsn,
        auth_jwt_secret="test-jwt-secret-not-for-production",
        access_token_ttl_seconds=3600,
        redis_url=None,
        whatsapp_webhook_secret=WEBHOOK_SECRET,
        whatsapp_allowed_senders="",
        rate_limit_enabled=True,
        log_level="WARNING",
        _env_file=None,
    )


@pytest.fixture
async def client(db_reset):
    app = create_app(make_settings(db_reset))
    app.state.test_dsn = db_reset
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.app = app
            yield c


@pytest.fixture
async def login(client):
    """Async helper: login(email) -> access_token (fresh per call)."""
    async def _login(email: str) -> str:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()["data"]["access_token"]

    return _login


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def unique_key() -> str:
    return f"idem-{secrets.token_hex(8)}"
