"""System status (contract §24) — real states only: adapter health, DB, scheduler."""
from __future__ import annotations


class SystemService:
    def __init__(self, db, adapters: dict, scheduler_running) -> None:
        self._db = db
        self._adapters = adapters
        self._scheduler_running = scheduler_running

    async def status(self) -> dict:
        integrations: dict[str, str] = {}
        for provider, adapter in self._adapters.items():
            state = adapter.health().state
            integrations[provider] = state
        db_ok = await self._db.health()
        all_connected = all(v == "CONNECTED" for v in integrations.values())
        system = "OPERATIONAL" if (db_ok and all_connected) else ("DEGRADED" if db_ok else "DOWN")
        return {
            "system": system,
            "agent": "READY",
            "automation": "ACTIVE" if self._scheduler_running() else "DISABLED",
            "security": "PROTECTED",
            "integrations": integrations,
        }
