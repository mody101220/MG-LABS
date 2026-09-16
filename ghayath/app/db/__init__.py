"""Async connection pool + DB facade. The DDL (sql/001_init.sql) is authoritative:
no ORM metadata, no auto-create — repositories issue parameterized SQL only."""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import repos


class DB:
    """Facade exposing one repository per domain."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool
        self.users = repos.UserRepository(self)
        self.tokens = repos.TokenRepository(self)
        self.conversations = repos.ConversationRepository(self)
        self.projects = repos.ProjectRepository(self)
        self.tasks = repos.TaskRepository(self)
        self.verifications = repos.VerificationRepository(self)
        self.commands = repos.CommandRepository(self)
        self.executions = repos.ExecutionRepository(self)
        self.approvals = repos.ApprovalRepository(self)
        self.automations = repos.AutomationRepository(self)
        self.notifications = repos.NotificationRepository(self)
        self.audit = repos.AuditRepository(self)
        self.incidents = repos.IncidentRepository(self)
        self.integrations = repos.IntegrationRepository(self)
        self.gmail_oauth = repos.GmailOAuthStateRepository(self)
        self.whatsapp = repos.WhatsAppRepository(self)
        self.email = repos.EmailRepository(self)
        self.github = repos.GitHubRepository(self)
        self.memory = repos.MemoryRepository(self)
        self.events = repos.EventRepository(self)
        self.idempotency = repos.IdempotencyRepository(self)

    async def fetchone(self, sql: str, *args) -> dict | None:
        params = args[0] if (len(args) == 1 and isinstance(args[0], (list, tuple))) else args or None
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row  # type: ignore[assignment]  # psycopg documented pattern
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetch(self, sql: str, *args) -> list[dict]:
        params = args[0] if (len(args) == 1 and isinstance(args[0], (list, tuple))) else args or None
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row  # type: ignore[assignment]  # psycopg documented pattern
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def execute(self, sql: str, *args) -> str:
        params = args[0] if (len(args) == 1 and isinstance(args[0], (list, tuple))) else args or None
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row  # type: ignore[assignment]  # psycopg documented pattern
            cur = await conn.execute(sql, params)
            return cur.statusmessage or ""

    async def health(self) -> bool:
        try:
            async with self.pool.connection() as conn:
                conn.row_factory = dict_row  # type: ignore[assignment]  # psycopg documented pattern
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.pool.close()


def make_pool(database_url: str, pool_size: int) -> AsyncConnectionPool:
    # open=False: the pool is opened explicitly (main lifespan / tests) — no
    # constructor-side opening (deprecated in psycopg >= 3.3).
    return AsyncConnectionPool(
        conninfo=database_url,
        min_size=2,
        max_size=pool_size,
        kwargs={"autocommit": False},
        open=False,
    )
