"""Schema migration runner. 001_init.sql remains the authoritative base schema;
additive migrations are explicit files and are only applied when their marker
table is absent. No ORM or generated DDL is used."""
from __future__ import annotations

import re
from pathlib import Path

from app.core import context
from psycopg.rows import dict_row

MIGRATION_FILE = Path(__file__).resolve().parents[2] / "sql" / "001_init.sql"
GMAIL_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "sql" / "002_gmail_oauth.sql"
_TX_RE = re.compile(r"^\s*(BEGIN|COMMIT);\s*$", re.M)


def _load(path: Path) -> str:
    return _TX_RE.sub("", path.read_text())


def load_migration_sql() -> str:
    """Compatibility loader for the authoritative base DDL."""
    return _load(MIGRATION_FILE)


def load_gmail_migration_sql() -> str:
    return _load(GMAIL_MIGRATION_FILE)


async def apply_migrations(pool) -> bool:
    """Apply the base schema and explicit additive migrations exactly once."""
    changed = False
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT to_regclass('public.users') IS NOT NULL AS installed")
        row = await cur.fetchone()
        if not row["installed"]:
            await conn.execute(load_migration_sql())
            context.log_event("migration.applied", {"file": MIGRATION_FILE.name})
            changed = True

        cur = await conn.execute("SELECT to_regclass('public.gmail_oauth_states') IS NOT NULL AS installed")
        row = await cur.fetchone()
        if not row["installed"]:
            await conn.execute(load_gmail_migration_sql())
            context.log_event("migration.applied", {"file": GMAIL_MIGRATION_FILE.name})
            changed = True
        if not changed:
            context.log_event("migration.skipped", {"reason": "already installed"})
    return changed
