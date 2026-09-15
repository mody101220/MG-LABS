"""Schema migration runner. The DDL file is the single source of truth for schema."""
from __future__ import annotations

import re
from pathlib import Path

from app.core import context
from psycopg.rows import dict_row

MIGRATION_FILE = Path(__file__).resolve().parents[2] / "sql" / "001_init.sql"

# The file is written for psql (top-level BEGIN;/COMMIT;). psycopg3 already runs the
# batch in a single implicit transaction, so strip the outer transaction markers.
_TX_RE = re.compile(r"^\s*(BEGIN|COMMIT);\s*$", re.M)


def load_migration_sql() -> str:
    return _TX_RE.sub("", MIGRATION_FILE.read_text())


async def apply_migrations(pool) -> bool:
    """Apply 001_init.sql if not yet installed. Returns True when a migration ran."""
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT to_regclass('public.users') IS NOT NULL AS installed")
        row = await cur.fetchone()
        if row["installed"]:
            context.log_event("migration.skipped", {"reason": "already installed"})
            return False
        await conn.execute(load_migration_sql())
        context.log_event("migration.applied", {"file": MIGRATION_FILE.name})
        return True
