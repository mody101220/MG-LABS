"""Operation security registry — feeds the contract-drift test (Phase 17).

Each router records its declared security level; the drift test compares it with the
security requirements declared in openapi.yaml (bearerAuth / [] / webhookSignature).
"""
from __future__ import annotations

from typing import Literal

SecurityLevel = Literal["PUBLIC", "WEBHOOK", "AUTH"]

# (method, path) -> security level, exactly as implemented by the routers.
REGISTRY: dict[tuple[str, str], SecurityLevel] = {}


def register(method: str, path: str, level: SecurityLevel) -> None:
    REGISTRY[(method.upper(), path)] = level


def reset() -> None:
    REGISTRY.clear()
