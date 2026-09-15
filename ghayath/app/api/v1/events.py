"""Authenticated Server-Sent Events feed backed by the persisted events table."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import StreamingResponse

from app.core.registry import register
from app.core.security import Principal, get_stream_principal

router = APIRouter(tags=["Events"])


def _rfc3339(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z") if hasattr(value, "isoformat") else str(value)


def _event_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["id"],
        "event": row["event"],
        "source": row["source"],
        "project_id": row.get("project_id"),
        "severity": row.get("severity"),
        "timestamp": _rfc3339(row["occurred_at"]),
        "payload": row.get("payload") or {},
    }


def _sse(row: dict[str, Any]) -> str:
    payload = json.dumps(_event_data(row), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"id: {row['id']}\nevent: {row['event']}\ndata: {payload}\n\n"


async def _stream(request: Request, since: str | None, types: set[str]) -> AsyncIterator[str]:
    events = request.app.state.db.events
    # Without a cursor the contract means "from now", not a replay of history.
    cursor = since if since is not None else await events.latest_id()
    heartbeat_at = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        rows = await events.after(cursor, 100)
        for row in rows:
            cursor = row["id"]
            if not types or row["event"] in types:
                yield _sse(row)
        now = time.monotonic()
        if now - heartbeat_at >= 15:
            yield ": ping\n\n"
            heartbeat_at = now
        await asyncio.sleep(2)


@router.get("/events/stream", operation_id="getEventsStream", response_class=StreamingResponse)
async def get_events_stream(
    request: Request,
    since: str | None = Query(default=None),
    types: str | None = Query(default=None),
    principal: Principal = Depends(get_stream_principal),
):
    del principal  # authentication/authorization is enforced by the dependency.
    event_types = {item.strip() for item in (types or "").split(",") if item.strip()}
    return StreamingResponse(
        _stream(request, since, event_types),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# Security registry (contract-drift test reads this at import time).
register("GET", "/events/stream", "AUTH")
