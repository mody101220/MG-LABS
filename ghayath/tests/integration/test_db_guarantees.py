"""Validation 5-6: the six database guarantees against real PostgreSQL 16:
  1. 001_init.sql applies cleanly
  2. migration is idempotent
  3. audit_logs is immutable (UPDATE/DELETE trigger)
  4. unique constraints (whatsapp external id, idempotency scope+key)
  5. FK + CHECK constraint enforcement
  6. audit log grows monotonically with important actions (append-only)
"""
from __future__ import annotations

import asyncio
import psycopg
import pytest

from app.db import make_pool
from app.db.migrate import apply_migrations

TABLE_COUNT = 25


async def test_migration_applies_cleanly_and_idempotently(pg):
    """Guarantees 1+2: fresh scratch database, apply twice, exact table count."""
    admin = psycopg.connect(pg, autocommit=True)
    try:
        admin.execute("DROP DATABASE IF EXISTS ghayath_schema_test")
        admin.execute("CREATE DATABASE ghayath_schema_test")
    finally:
        admin.close()
    head, _, query = pg.partition("?")
    dsn = head[: head.rfind("/")] + "/ghayath_schema_test" + ("?" + query if query else "")
    pool = make_pool(dsn, pool_size=2)
    try:
        await pool.wait()
        ran1 = await apply_migrations(pool)
        ran2 = await apply_migrations(pool)  # must be a no-op
        assert ran1 is True
        assert ran2 is False
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            row = await cur.fetchone()
        assert row["n"] == TABLE_COUNT
    finally:
        await pool.close()
        admin = psycopg.connect(pg, autocommit=True)
        admin.execute("DROP DATABASE IF EXISTS ghayath_schema_test")
        admin.close()


def test_audit_logs_are_immutable(db_reset):
    """Guarantee 3: no UPDATE/DELETE on audit_logs — trigger raises."""
    conn = psycopg.connect(db_reset, autocommit=True)
    try:
        conn.execute(
            "INSERT INTO audit_logs (actor_type, actor, action, result) VALUES ('system', 'system', 'TEST', 'SUCCESS')")
        row = conn.execute("SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit_logs SET action = 'TAMPERED' WHERE id = %s", (row[0],))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM audit_logs WHERE id = %s", (row[0],))
        # unchanged
        r = conn.execute("SELECT action FROM audit_logs WHERE id = %s", (row[0],)).fetchone()
        assert r[0] == "TEST"
        n = conn.execute("SELECT count(*) FROM audit_logs WHERE id = %s", (row[0],)).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_unique_constraints(db_reset):
    """Guarantee 4: provider message ids (wamid dedupe) and idempotency scope+key are unique."""
    conn = psycopg.connect(db_reset, autocommit=True)
    try:
        conn.execute(
            "INSERT INTO whatsapp_messages (id, external_id, direction, sender, body, status) "
            "VALUES ('wmsg_uq001', 'wamid_same', 'INBOUND', '+11122233344', 'hi', 'DELIVERED')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO whatsapp_messages (id, external_id, direction, sender, body, status) "
                "VALUES ('wmsg_uq002', 'wamid_same', 'INBOUND', '+11122233344', 'hi', 'DELIVERED')")
        n = conn.execute("SELECT count(*) FROM whatsapp_messages WHERE external_id = 'wamid_same'").fetchone()[0]
        assert n == 1
        # idempotency (scope, key) unique
        conn.execute(
            "INSERT INTO idempotency_keys (scope, key, request_hash, response_code, response_body) "
            "VALUES ('t', 'k1', 'h', 200, '{}'::jsonb)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO idempotency_keys (scope, key, request_hash, response_code, response_body) "
                "VALUES ('t', 'k1', 'h2', 200, '{}'::jsonb)")
    finally:
        conn.close()


def test_fk_and_check_constraints(db_reset):
    """Guarantee 5: referential + domain constraints reject invalid rows."""
    conn = psycopg.connect(db_reset, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, priority, status) VALUES ('ts_fk1', 'prj_missing', 't', 'P1', 'TODO')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO users (id, email, password_hash, role) VALUES ('usr_ck', 'a@b.c', 'x', 'ROOT')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, priority, status) VALUES ('ts_ck', 'prj_ck', 't', 'P1', 'WEIRD')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO audit_logs (actor_type, actor, action, result) VALUES ('system', 's', 'a', 'EXPLOITED')")
    finally:
        conn.close()


async def test_audit_append_only_growth(client):
    """Guarantee 6: important actions append audit rows; ids monotonically increase."""
    t = await client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "Sup3rSecret!x"})
    tok = t.json()["data"]["access_token"]
    from tests.conftest import bearer

    before = await client.app.state.db.fetchone("SELECT coalesce(max(id),0) AS m FROM audit_logs")
    await client.post("/api/v1/projects", headers=bearer(tok), json={"name": "Audit growth", "priority": "P1"})
    after = await client.app.state.db.fetchone("SELECT max(id) AS m FROM audit_logs")
    assert after["m"] > before["m"]
