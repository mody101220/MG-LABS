"""Repositories: parameterized SQL only. The DDL (constraints/FKs/triggers) is the
source of truth; repositories never bypass it and never mutate audit_logs beyond INSERT."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app.services.ids import new_id
from typing import Any

from psycopg.rows import dict_row

J = json.dumps


def _params(args: tuple[Any, ...]) -> tuple[Any, ...] | None:
    if not args:
        return None
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        return tuple(args[0])
    return args


class _Repo:
    def __init__(self, db) -> None:
        self._db = db

    async def fetchone(self, sql: str, *args: Any) -> dict | None:
        async with self._db.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(sql, _params(args))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchone_one(self, sql: str, *args: Any) -> dict:
        """Exactly one row is guaranteed by the query shape (count/RETURNING)."""
        row = await self.fetchone(sql, *args)
        assert row is not None
        return row

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        async with self._db.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(sql, _params(args))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def execute(self, sql: str, *args: Any) -> str:
        async with self._db.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(sql, _params(args))
            return cur.statusmessage


# ─────────────────────────── Auth / identity ───────────────────────────

class UserRepository(_Repo):
    async def create(self, id: str, email: str, password_hash: str, role: str, name: str | None = None) -> dict:
        row = await self.fetchone_one(
            "INSERT INTO users (id, email, password_hash, role, name) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            id, email, password_hash, role, name,
        )
        return row

    async def get_by_id(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM users WHERE id = %s", id)

    async def get_by_email(self, email: str) -> dict | None:
        return await self.fetchone("SELECT * FROM users WHERE lower(email) = lower(%s)", email)


class TokenRepository(_Repo):
    async def create(self, id: str, user_id: str, token_hash: str, expires_at: datetime) -> None:
        await self.execute(
            "INSERT INTO auth_tokens (id, user_id, token_hash, expires_at) VALUES (%s,%s,%s,%s)",
            id, user_id, token_hash, expires_at,
        )

    async def find_by_hash(self, token_hash: str) -> dict | None:
        return await self.fetchone("SELECT * FROM auth_tokens WHERE token_hash = %s", token_hash)

    async def touch(self, token_hash: str) -> None:
        await self.execute("UPDATE auth_tokens SET last_used_at = now() WHERE token_hash = %s", token_hash)

    async def revoke(self, token_hash: str) -> None:
        await self.execute("UPDATE auth_tokens SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL", token_hash)

    async def revoke_all_for_user(self, user_id: str) -> None:
        await self.execute("UPDATE auth_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", user_id)

    async def revoke_access_jti(self, user_id: str, jti: str, ttl_seconds: int) -> None:
        """Record a revoked access token (jti) for the lifetime of the token."""
        from datetime import datetime, timedelta, timezone

        from app.services.ids import new_id as _new_id
        await self.execute(
            "INSERT INTO auth_tokens (id, user_id, token_hash, expires_at, revoked_at) "
            "VALUES (%s,%s,%s,%s, now()) ON CONFLICT (token_hash) DO NOTHING",
            _new_id("tok"), user_id, jti,
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )


class ConversationRepository(_Repo):
    async def get_or_create(self, id: str, user_id: str) -> None:
        await self.execute(
            "INSERT INTO conversations (id, user_id) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING", id, user_id,
        )

    async def add_message(self, conversation_id: str, role: str, content: str, command_id: str | None) -> None:
        await self.execute(
            "INSERT INTO conversation_messages (conversation_id, role, content, command_id) VALUES (%s,%s,%s,%s)",
            conversation_id, role, content, command_id,
        )

    async def history(self, conversation_id: str, limit: int = 10) -> list[dict]:
        """Most recent messages (chronological order) for prompt context."""
        rows = await self.fetch(
            "SELECT role, content, created_at FROM conversation_messages "
            "WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
            conversation_id, limit,
        )
        return rows[::-1]


# ─────────────────────────── Projects ───────────────────────────

class ProjectRepository(_Repo):
    _UPDATABLE = {"status", "version", "priority", "description", "next_action", "repository_url", "deployment_url"}

    async def list(self, page: int, size: int) -> tuple[list[dict], int]:
        total = await self.fetchone_one("SELECT count(*)::int AS n FROM projects")
        rows = await self.fetch(
            "SELECT id, name, status, priority, version FROM projects ORDER BY last_activity_at DESC NULLS LAST, name LIMIT %s OFFSET %s",
            size, (page - 1) * size,
        )
        return rows, total["n"]

    async def create(self, id: str, name: str, slug: str, priority: str, description: str | None,
                     version: str, repository_url: str | None, deployment_url: str | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO projects (id, name, slug, priority, description, version, repository_url, deployment_url)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, name, slug, priority, description, version, repository_url, deployment_url,
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM projects WHERE id = %s", id)

    async def update(self, id: str, fields: dict[str, Any]) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE and v is not None}
        if not fields:
            return await self.get(id)
        sets = ", ".join(f"{k} = %s" for k in fields)
        row = await self.fetchone(f"UPDATE projects SET {sets} WHERE id = %s RETURNING *", *fields.values(), id)
        return row

    async def touch_activity(self, id: str) -> None:
        await self.execute("UPDATE projects SET last_activity_at = now() WHERE id = %s", id)

    async def detail(self, id: str) -> dict | None:
        p = await self.get(id)
        if not p:
            return None
        tasks = await self.fetchone(
            "SELECT count(*)::int AS total, count(*) FILTER (WHERE status NOT IN ('DONE','CANCELLED'))::int AS open, "
            "count(*) FILTER (WHERE due_at IS NOT NULL AND due_at < now() AND status NOT IN ('DONE','CANCELLED'))::int AS overdue "
            "FROM tasks WHERE project_id = %s", id)
        repo = await self.fetchone(
            "SELECT r.id AS repo_id, r.full_name, r.default_branch, r.last_checked_at "
            "FROM github_repositories r WHERE r.project_id = %s LIMIT 1", id)
        integ = await self.fetch(
            """SELECT pi.provider, i.status FROM project_integrations pi
               LEFT JOIN integrations i ON i.id = pi.provider WHERE pi.project_id = %s""", id)
        bugs = await self.fetchone(
            """SELECT count(*) FILTER (WHERE i.status = 'OPEN')::int AS open,
                      count(*) FILTER (WHERE i.status = 'OPEN' AND i.priority IN ('P0','P1'))::int AS critical
               FROM github_issues i JOIN github_repositories r ON r.id = i.repository_id
               WHERE r.project_id = %s""", id)
        p.update(tasks=tasks, repository=repo, integrations=integ, bugs=bugs)
        return p


# ─────────────────────────── Tasks / verifications ───────────────────────────

class TaskRepository(_Repo):
    _UPDATABLE = {"title", "description", "priority", "status", "assignee", "due_at"}

    async def create(self, id: str, project_id: str, title: str, priority: str, status: str,
                     description: str | None, assignee: str | None, due_at: datetime | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO tasks (id, project_id, title, description, priority, status, assignee, due_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, project_id, title, description, priority, status, assignee, due_at,
        )

    async def list(self, project_id: str | None, status: str | None, priority: str | None,
                   assignee: str | None, due_before: datetime | None, page: int, size: int) -> tuple[list[dict], int]:
        where: list = ["1=1"]
        args: list = []
        if project_id:
            where.append("project_id = %s"); args.append(project_id)
        if status:
            where.append("status = %s"); args.append(status)
        if priority:
            where.append("priority = %s"); args.append(priority)
        if assignee:
            where.append("assignee = %s"); args.append(assignee)
        if due_before:
            where.append("due_at IS NOT NULL AND due_at < %s"); args.append(due_before)
        w = " AND ".join(where)
        total = await self.fetchone_one(f"SELECT count(*)::int AS n FROM tasks WHERE {w}", *args)
        rows = await self.fetch(
            f"SELECT * FROM tasks WHERE {w} ORDER BY (due_at IS NULL), due_at, priority, created_at LIMIT %s OFFSET %s",
            *args, size, (page - 1) * size)
        return rows, total["n"]

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM tasks WHERE id = %s", id)

    async def update(self, id: str, fields: dict[str, Any]) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE and v is not None}
        if not fields:
            return await self.get(id)
        sets = ", ".join(f"{k} = %s" for k in fields)
        return await self.fetchone(f"UPDATE tasks SET {sets} WHERE id = %s RETURNING *", *fields.values(), id)

    async def mark_done(self, id: str) -> dict | None:
        return await self.fetchone(
            "UPDATE tasks SET status = 'DONE', completed_at = now() WHERE id = %s RETURNING *", id)


class VerificationRepository(_Repo):
    async def create(self, id: str, subject_type: str, subject_id: str, required: bool) -> dict | None:
        return await self.fetchone(
            "INSERT INTO verifications (id, subject_type, subject_id, required) VALUES (%s,%s,%s,%s) RETURNING *",
            id, subject_type, subject_id, required,
        )

    async def update_status(self, id: str, status: str, verified_by: str | None, checks: list | None) -> dict | None:
        return await self.fetchone(
            "UPDATE verifications SET status = %s, verified_by = %s, checks = %s, verified_at = now() WHERE id = %s RETURNING *",
            status, verified_by, J(checks or []), id,
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM verifications WHERE id = %s", id)


# ─────────────────────────── Agent: commands / executions ───────────────────────────

class CommandRepository(_Repo):
    async def create(self, id: str, conversation_id: str | None, user_id: str | None, source: str,
                     command_text: str, intent: str, autonomous: bool, plan: list) -> dict | None:
        return await self.fetchone(
            """INSERT INTO agent_commands (id, conversation_id, user_id, source, command_text, intent, autonomous, plan)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, conversation_id, user_id, source, command_text, intent, autonomous, J(plan),
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM agent_commands WHERE id = %s", id)

    async def update_status(self, id: str, status: str) -> dict | None:
        return await self.fetchone("UPDATE agent_commands SET status = %s WHERE id = %s RETURNING *", status, id)

    async def update_plan(self, id: str, plan: list) -> dict | None:
        return await self.fetchone("UPDATE agent_commands SET plan = %s WHERE id = %s RETURNING *", J(plan), id)

    async def set_approval(self, id: str, approval_id: str) -> None:
        await self.execute("UPDATE agent_commands SET approval_id = %s WHERE id = %s", approval_id, id)


class ExecutionRepository(_Repo):
    async def create(self, id: str, command_id: str, approval_id: str | None) -> dict | None:
        return await self.fetchone(
            "INSERT INTO executions (id, command_id, approval_id) VALUES (%s,%s,%s) RETURNING *",
            id, command_id, approval_id,
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM executions WHERE id = %s", id)

    async def set_status(self, id: str, status: str) -> None:
        await self.execute("UPDATE executions SET status = %s WHERE id = %s", status, id)

    async def finish(self, id: str, status: str, error_code: str | None, error_message: str | None,
                     verification_id: str | None, result: dict) -> dict | None:
        return await self.fetchone(
            """UPDATE executions SET status = %s, error_code = %s, error_message = %s,
               verification_id = %s, result = %s, finished_at = now() WHERE id = %s RETURNING *""",
            status, error_code, error_message, verification_id, J(result), id,
        )


# ─────────────────────────── Approvals ───────────────────────────

class ApprovalRepository(_Repo):
    async def create(self, id: str, type: str, payload: dict, request_reason: str | None,
                     requested_by: str, expires_at: datetime | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO approvals (id, type, payload, request_reason, requested_by, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, type, J(payload), request_reason, requested_by, expires_at,
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM approvals WHERE id = %s", id)

    async def list(self, status: str | None, type: str | None, page: int, size: int) -> tuple[list[dict], int]:
        where: list = ["1=1"]
        args: list = []
        if status:
            where.append("status = %s"); args.append(status)
        if type:
            where.append("type = %s"); args.append(type)
        w = " AND ".join(where)
        total = await self.fetchone_one(f"SELECT count(*)::int AS n FROM approvals WHERE {w}", *args)
        rows = await self.fetch(f"SELECT * FROM approvals WHERE {w} ORDER BY requested_at DESC LIMIT %s OFFSET %s",
                                *args, size, (page - 1) * size)
        return rows, total["n"]

    async def decide(self, id: str, status: str, decided_by: str, reason: str | None) -> dict | None:
        return await self.fetchone(
            "UPDATE approvals SET status = %s, decided_by = %s, decision_reason = %s, decided_at = now() "
            "WHERE id = %s AND status = 'PENDING' RETURNING *",
            status, decided_by, reason, id,
        )

    async def find_granted(self, type: str, payload: dict) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM approvals WHERE type = %s AND payload = %s::jsonb AND status = 'APPROVED' "
            "ORDER BY decided_at DESC LIMIT 1",
            type, J(payload),
        )

    async def expire_stale(self) -> None:
        """30-min TTL (contract §17): PENDING approvals past their expiry cannot be granted."""
        await self.execute(
            "UPDATE approvals SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at IS NOT NULL AND expires_at < now()")


# ─────────────────────────── Automations ───────────────────────────

class AutomationRepository(_Repo):
    async def create(self, id: str, name: str, description: str | None, trigger: dict, action: dict,
                     enabled: bool, next_run_at: datetime | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO automations (id, name, description, trigger, action, enabled, next_run_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, name, description, J(trigger), J(action), enabled, next_run_at,
        )

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM automations WHERE id = %s", id)

    async def update(self, id: str, name: str | None, description: str | None, trigger: dict | None,
                     action: dict | None, enabled: bool | None, next_run_at: datetime | None = None) -> dict | None:
        sets: list = []
        args: list = []
        if name is not None:
            sets.append("name = %s"); args.append(name)
        if description is not None:
            sets.append("description = %s"); args.append(description)
        if trigger is not None:
            sets.append("trigger = %s"); args.append(J(trigger))
        if action is not None:
            sets.append("action = %s"); args.append(J(action))
        if enabled is not None:
            sets.append("enabled = %s"); args.append(enabled)
        if next_run_at is not None:
            sets.append("next_run_at = %s"); args.append(next_run_at)
        if not sets:
            return await self.get(id)
        args.append(id)
        return await self.fetchone(f"UPDATE automations SET {', '.join(sets)} WHERE id = %s RETURNING *", *args)

    async def due(self, now: datetime) -> list[dict]:
        return await self.fetch(
            "SELECT * FROM automations WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= %s", now)

    async def mark_run(self, id: str, status: str, next_run_at: datetime | None) -> None:
        await self.execute(
            "UPDATE automations SET last_run_at = now(), last_run_status = %s, next_run_at = %s WHERE id = %s",
            status, next_run_at, id)


# ─────────────────────────── Notifications ───────────────────────────

class NotificationRepository(_Repo):
    async def create(self, id: str, severity: str, channel_requested: str | None, channels: list,
                     title: str, message: str, project_id: str | None, status: str, data: dict | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO notifications (id, severity, channel_requested, channels, title, message, project_id, status, data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, severity, channel_requested, J(channels), title, message, project_id, status, J(data or {}),
        )


# ─────────────────────────── Audit (INSERT only) ───────────────────────────

class AuditRepository(_Repo):
    async def log(self, actor_type: str, actor: str, action: str, result: str,
                  target_type: str | None = None, target_id: str | None = None,
                  project_id: str | None = None, execution_id: str | None = None,
                  command_id: str | None = None, request_id: str | None = None,
                  details: dict | None = None) -> int:
        row = await self.fetchone_one(
            """INSERT INTO audit_logs (actor_type, actor, action, result, target_type, target_id,
               project_id, execution_id, command_id, request_id, details)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            actor_type, actor, action, result, target_type, target_id, project_id,
            execution_id, command_id, request_id, J(details or {}),
        )
        return row["id"]

    async def list(self, project_id: str | None, action: str | None, actor: str | None,
                   result: str | None, from_: datetime | None, to: datetime | None,
                   page: int, size: int) -> tuple[list[dict], int]:
        where: list = ["1=1"]
        args: list = []
        if project_id:
            where.append("project_id = %s"); args.append(project_id)
        if action:
            where.append("action = %s"); args.append(action)
        if actor:
            where.append("actor = %s"); args.append(actor)
        if result:
            where.append("result = %s"); args.append(result)
        if from_:
            where.append("occurred_at >= %s"); args.append(from_)
        if to:
            where.append("occurred_at <= %s"); args.append(to)
        w = " AND ".join(where)
        total = await self.fetchone_one(f"SELECT count(*)::int AS n FROM audit_logs WHERE {w}", *args)
        rows = await self.fetch(f"SELECT * FROM audit_logs WHERE {w} ORDER BY id DESC LIMIT %s OFFSET %s",
                                *args, size, (page - 1) * size)
        return rows, total["n"]


# ─────────────────────────── Incidents ───────────────────────────

class IncidentRepository(_Repo):
    async def create(self, id: str, severity: str, system: str, issue: str, impact: str | None,
                     source: str) -> dict | None:
        return await self.fetchone(
            "INSERT INTO incidents (id, severity, system, issue, impact, source) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            id, severity, system, issue, impact, source,
        )


# ─────────────────────────── Integrations ───────────────────────────

class IntegrationRepository(_Repo):
    async def get(self, provider: str) -> dict | None:
        return await self.fetchone("SELECT * FROM integrations WHERE id = %s", provider)

    async def list(self) -> list[dict]:
        return await self.fetch("SELECT * FROM integrations ORDER BY id")

    async def set_status(self, provider: str, status: str) -> None:
        await self.execute("UPDATE integrations SET status = %s, last_checked_at = now() WHERE id = %s", status, provider)


# ─────────────────────────── WhatsApp ───────────────────────────

class WhatsAppRepository(_Repo):
    async def insert_inbound(self, external_id: str, sender: str, body: str) -> dict | None:
        """Returns None when the wamid was already stored (dedupe)."""
        row = await self.fetchone(
            """INSERT INTO whatsapp_messages (id, external_id, direction, sender, body)
               VALUES (%s, %s, 'INBOUND', %s, %s)
               ON CONFLICT (external_id) DO NOTHING RETURNING *""",
            new_id("wmsg"), external_id, sender, body,
        )
        return row

    async def get_by_external_id(self, external_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM whatsapp_messages WHERE external_id = %s", external_id)

    async def insert_outbound(self, id: str, recipient: str, body: str, idempotency_key: str | None,
                              approval_id: str | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO whatsapp_messages (id, direction, recipient, body, idempotency_key, approval_id, status, sent_at)
               VALUES (%s, 'OUTBOUND', %s, %s, %s, %s, 'SENT', now()) RETURNING *""",
            id, recipient, body, idempotency_key, approval_id,
        )

    async def update_status(self, id: str, status: str) -> None:
        await self.execute("UPDATE whatsapp_messages SET status = %s WHERE id = %s", status, id)

    async def list_inbound(self, page: int, size: int) -> tuple[list[dict], int]:
        total = await self.fetchone_one("SELECT count(*)::int AS n FROM whatsapp_messages WHERE direction = 'INBOUND'")
        rows = await self.fetch(
            "SELECT * FROM whatsapp_messages WHERE direction = 'INBOUND' ORDER BY created_at DESC LIMIT %s OFFSET %s",
            size, (page - 1) * size)
        return rows, total["n"]

    async def list_outbound(self, limit: int) -> list[dict]:
        return await self.fetch(
            "SELECT * FROM whatsapp_messages WHERE direction = 'OUTBOUND' ORDER BY created_at DESC LIMIT %s", limit)


# ─────────────────────────── Email ───────────────────────────

class EmailRepository(_Repo):
    async def list_messages(self, folder: str | None, priority: str | None,
                            page: int, size: int) -> tuple[list[dict], int]:
        where: list = ["1=1"]
        args: list = []
        if folder:
            where.append("folder = %s"); args.append(folder)
        if priority:
            where.append("priority = %s"); args.append(priority)
        w = " AND ".join(where)
        total = await self.fetchone_one(f"SELECT count(*)::int AS n FROM email_messages WHERE {w}", *args)
        rows = await self.fetch(
            f"SELECT id, folder, COALESCE(sender, 'unknown@example.com') AS sender, "
            f"COALESCE(subject, '') AS subject, priority, "
            f"COALESCE(classification, 'UNKNOWN') AS classification, COALESCE(summary, '') AS summary, "
            f"required_action, deadline, COALESCE(received_at, created_at) AS received_at "
            f"FROM email_messages WHERE {w} ORDER BY COALESCE(received_at, created_at) DESC LIMIT %s OFFSET %s",
            *args, size, (page - 1) * size)
        return rows, total["n"]

    async def get_message(self, id: str) -> dict | None:
        return await self.fetchone(
            "SELECT id, folder, COALESCE(sender, 'unknown@example.com') AS sender, "
            "COALESCE(subject, '') AS subject, priority, COALESCE(classification, 'UNKNOWN') AS classification, "
            "COALESCE(summary, '') AS summary, required_action, deadline, "
            "COALESCE(received_at, created_at) AS received_at, COALESCE(body, '') AS body, is_read "
            "FROM email_messages WHERE id = %s", id)

    async def create_draft(self, id: str, reply_to: str | None, to_addr: str, subject: str | None, body: str) -> dict | None:
        return await self.fetchone(
            "INSERT INTO email_drafts (id, reply_to, to_addr, subject, body) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            id, reply_to, to_addr, subject, body,
        )

    async def create_sent_message(self, id: str, to_addr: str, subject: str | None, body: str | None,
                                  external_id: str | None) -> dict | None:
        return await self.fetchone(
            "INSERT INTO email_messages (id, external_id, folder, sender, subject, body, priority, received_at) "
            "VALUES (%s, %s, 'sent', %s, %s, %s, 'NORMAL', now()) RETURNING *",
            id, external_id, to_addr, subject, body)

    async def get_draft(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM email_drafts WHERE id = %s", id)

    async def update_draft(self, id: str, status: str, approval_id: str | None = None,
                           sent_message_id: str | None = None) -> dict | None:
        return await self.fetchone(
            """UPDATE email_drafts SET status = %s, approval_id = COALESCE(%s, approval_id),
               sent_message_id = COALESCE(%s, sent_message_id) WHERE id = %s RETURNING *""",
            status, approval_id, sent_message_id, id,
        )


# ─────────────────────────── GitHub ───────────────────────────

class GitHubRepository(_Repo):
    async def list_repos(self, page: int, size: int) -> tuple[list[dict], int]:
        total = await self.fetchone_one("SELECT count(*)::int AS n FROM github_repositories")
        rows = await self.fetch("SELECT * FROM github_repositories ORDER BY full_name LIMIT %s OFFSET %s",
                                size, (page - 1) * size)
        return rows, total["n"]

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM github_repositories WHERE id = %s", id)

    async def get_by_full_name(self, full_name: str) -> dict | None:
        return await self.fetchone("SELECT * FROM github_repositories WHERE full_name = %s", full_name)

    async def latest_snapshot(self, repo_id: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM repo_status_snapshots WHERE repo_id = %s ORDER BY checked_at DESC LIMIT 1", repo_id)

    async def create_issue(self, id: str, repository_id: str, number: int, title: str, body: str | None,
                           priority: str | None, url: str | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO github_issues (id, repository_id, number, title, body, priority, url)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            id, repository_id, number, title, body, priority, url,
        )


# ─────────────────────────── Memory ───────────────────────────

class MemoryRepository(_Repo):
    async def upsert(self, type: str, key: str, value: Any, project_id: str | None,
                     source: str, confidence: float | None, expires_at: datetime | None) -> dict | None:
        return await self.fetchone(
            """INSERT INTO memory (id, type, key, value, project_id, source, confidence, expires_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
               ON CONFLICT (type, key, (COALESCE(project_id, '')))
               DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source,
                             confidence = EXCLUDED.confidence, expires_at = EXCLUDED.expires_at
               RETURNING *""",
            f"mem_{hashlib.sha256((':'.join([type, key, project_id or ''])).encode()).hexdigest()[:16]}",
            type, key, J(value), project_id, source, confidence, expires_at,
        )

    async def list(self, type: str | None, project_id: str | None, key: str | None,
                   page: int, size: int) -> tuple[list[dict], int]:
        where: list = ["1=1"]
        args: list = []
        if type:
            where.append("type = %s"); args.append(type)
        if project_id:
            where.append("project_id = %s"); args.append(project_id)
        if key:
            where.append("key = %s"); args.append(key)
        w = " AND ".join(where)
        total = await self.fetchone_one(f"SELECT count(*)::int AS n FROM memory WHERE {w}", *args)
        rows = await self.fetch(f"SELECT * FROM memory WHERE {w} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                                *args, size, (page - 1) * size)
        return rows, total["n"]

    async def get(self, id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM memory WHERE id = %s", id)

    async def delete(self, id: str) -> dict | None:
        return await self.fetchone("DELETE FROM memory WHERE id = %s RETURNING *", id)


# ─────────────────────────── Events / idempotency ───────────────────────────

class EventRepository(_Repo):
    async def publish(self, id: str, event: str, source: str, project_id: str | None,
                      severity: str | None, payload: dict) -> dict | None:
        return await self.fetchone(
            "INSERT INTO events (id, event, source, project_id, severity, payload) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            id, event, source, project_id, severity, J(payload),
        )


class IdempotencyRepository(_Repo):
    async def lookup(self, scope: str, key: str) -> dict | None:
        return await self.fetchone("SELECT * FROM idempotency_keys WHERE scope = %s AND key = %s", scope, key)

    async def store(self, scope: str, key: str, request_hash: str, response_code: int, response_body: dict) -> None:
        await self.execute(
            """INSERT INTO idempotency_keys (scope, key, request_hash, response_code, response_body)
               VALUES (%s,%s,%s,%s,%s)""",
            scope, key, request_hash, response_code, J(response_body),
        )

    async def purge_expired(self) -> None:
        await self.execute("DELETE FROM idempotency_keys WHERE expires_at < now()")
