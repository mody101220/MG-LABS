"""Idempotency service (contract §27 / Phase 8).

For the five side-effect operations, a request carrying an Idempotency-Key:
  - first request  → executes, stores (scope, key, request_hash, status, body)
  - same key+body  → returns the stored response; the side effect is NOT repeated
  - same key, different body → 409 CONFLICT (key collision)
Storage: idempotency_keys (24h TTL per the DDL).
"""
from __future__ import annotations

import hashlib
import json

from starlette.responses import JSONResponse

from app.core.errors import conflict


class IdempotencyService:
    def __init__(self, db) -> None:
        self._db = db

    @staticmethod
    def _hash(body: dict) -> str:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def check(self, scope: str, key: str, body: dict) -> JSONResponse | None:
        row = await self._db.idempotency.lookup(scope, key)
        if row is None:
            return None
        if row["request_hash"] != self._hash(body):
            raise conflict("Idempotency-Key was already used with a different request body",
                           {"scope": scope, "key": key})
        return JSONResponse(row["response_body"], status_code=row["response_code"])

    async def record(self, scope: str, key: str, body: dict, status_code: int, response) -> None:
        content = response if isinstance(response, dict) else response.model_dump(mode="json")
        payload = json.dumps(content, ensure_ascii=False, default=str).replace("+00:00", "Z")
        try:
            await self._db.idempotency.store(scope, key, self._hash(body), status_code, json.loads(payload))
        except Exception:
            # A concurrent first-writer already stored the result; the unique
            # (scope, key) constraint is the race guard — nothing to do here.
            pass
