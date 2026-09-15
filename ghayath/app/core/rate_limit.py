"""Rate limiting exactly as contracted (contract §28):

- /auth/*            → 5 requests/minute
- /agent/command,
  /agent/execute     → 60 requests/minute
- WhatsApp/Email     → provider-dependent (NOT gateway-enforced here)
- Monitoring         → scheduler-paced (NOT gateway-enforced here)

On excess: HTTP 429 with the error envelope (RATE_LIMITED) + X-RateLimit-Reset header.
No undocumented limits are introduced.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from app.core import context
from app.core.envelope import error_payload
from app.core.errors import rate_limited

AUTH_LIMIT = 5
AGENT_LIMIT = 60
WINDOW_SECONDS = 60


@dataclass
class Limit:
    bucket: str
    limit: int


def bucket_for(path: str) -> Limit | None:
    if path.startswith("/api/v1/auth/"):
        return Limit("auth", AUTH_LIMIT)
    if path in ("/api/v1/agent/command", "/api/v1/agent/execute"):
        return Limit("agent", AGENT_LIMIT)
    return None


class MemoryStore:
    """Single-process store (tests / redis-less deployments)."""

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}

    def incr(self, key: str, window: int) -> tuple[int, int]:
        now = time.monotonic()
        count, start = self._counters.get(key, (0, now))
        if now - start >= window:
            count, start = 0, now
        count += 1
        self._counters[key] = (count, start)
        return count, max(0, window - int(now - start))


class RedisStore:
    def __init__(self, client) -> None:
        self._client = client

    def incr(self, key: str, window: int) -> tuple[int, int]:
        rkey = f"ghayath:rl:{key}"
        count = self._client.incr(rkey)
        if count == 1:
            self._client.expire(rkey, window)
        ttl = self._client.ttl(rkey)
        return count, (ttl if ttl and ttl > 0 else window)


def make_store(settings) -> MemoryStore | RedisStore:
    if settings.redis_url:
        try:
            import redis as redis_lib

            client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
            client.ping()
            return RedisStore(client)
        except Exception:
            # Fall back rather than break the API; health still reports redis down.
            context.log_event("ratelimit.redis_unavailable", {"fallback": "memory"})
    return MemoryStore()


def install_rate_limiter(app: FastAPI) -> None:
    limiter_store = None

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next: Callable[..., Awaitable[Response]]) -> Response:
        nonlocal limiter_store
        settings = request.app.state.settings
        if not settings.rate_limit_enabled:
            return await call_next(request)
        bucket = bucket_for(request.url.path)
        if bucket is None:
            return await call_next(request)
        if limiter_store is None:
            limiter_store = make_store(settings)
        principal = getattr(request.state, "principal", None)
        scope_key = principal.user_id if principal else (request.client.host if request.client else "anon")
        count, reset = limiter_store.incr(f"{bucket.bucket}:{scope_key}", WINDOW_SECONDS)
        if count > bucket.limit:
            err = rate_limited()
            resp = JSONResponse(status_code=429, content=error_payload(err.code, err.message, err.details, context.request_id_var.get() or context.new_request_id()))
            resp.headers["X-RateLimit-Reset"] = str(reset)
            return resp
        return await call_next(request)
