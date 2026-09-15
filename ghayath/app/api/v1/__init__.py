"""API v1 — assembles every router registered in the contract."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agent,
    approvals,
    audit,
    automations,
    auth,
    brief,
    email,
    github,
    incidents,
    memory,
    monitoring,
    notifications,
    permissions,
    projects,
    system,
    tasks,
    webhooks,
    whatsapp,
)


def create_v1_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    for module in (
        auth, agent, projects, tasks, memory, whatsapp, webhooks, email, github,
        monitoring, automations, approvals, permissions, notifications, audit,
        incidents, system, brief,
    ):
        router.include_router(module.router)
    return router
