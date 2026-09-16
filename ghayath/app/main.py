"""GHAYATH PERSONAL AI — FastAPI application factory.

Wires: config → DB (DDL-migrated) → services → agent pipeline → routers →
middleware (request context + rate limiting) → exception handlers (envelope).
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.agent.brain import AgentBrain
from app.agent.permission import PermissionEngine
from app.agent.verification import VerificationEngine
from app.api.v1 import create_v1_router
from app.core import context
from app.core.config import Settings
from app.core.context import setup_logging
from app.core.envelope import install_exception_handlers
from app.core.rate_limit import install_rate_limiter
from app.core.scheduler import Scheduler
from app.core.security import hash_password
from app.db import DB, make_pool
from app.db.migrate import apply_migrations
from app.integrations.adapters import EmailAdapter, GitHubAdapter, WhatsAppAdapter
from app.integrations.gmail import GmailHTTPAdapter
from app.integrations.llm import OpenAICompatibleLLM
from app.services.agent_service import AgentService
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.automation_service import AutomationService
from app.services.auth_service import AuthService
from app.services.brief_service import BriefService
from app.services.email_service import EmailService
from app.services.events import EventBus
from app.services.execution_service import ExecutionService
from app.services.github_service import GitHubService
from app.services.gmail_service import GmailService
from app.services.idempotency import IdempotencyService
from app.services.incident_service import IncidentService
from app.services.memory_service import MemoryService
from app.services.monitoring_service import MonitoringService
from app.services.notification_service import NotificationService
from app.services.permission_service import PermissionService
from app.services.project_service import ProjectService
from app.services.system_service import SystemService
from app.services.task_service import TaskService
from app.services.whatsapp_service import WhatsAppService
from app.services.ids import new_id


class ZJSONResponse(JSONResponse):
    """Contract time format: ISO-8601 UTC with 'Z' suffix."""

    def render(self, content) -> bytes:
        text = json.dumps(content, ensure_ascii=False, default=str)
        return text.replace("+00:00", "Z").encode("utf-8")


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    """Application factory. `llm` (an OpenAICompatibleLLM) is injectable for
    tests; production builds one from GHAYATH_LLM_* environment settings."""
    settings = settings or Settings()
    if not settings.auth_jwt_secret:
        raise RuntimeError("GHAYATH_AUTH_JWT_SECRET must be set")
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = make_pool(settings.database_url, settings.db_pool_size)
        await pool.open()
        await pool.wait()
        db = DB(pool)
        await apply_migrations(pool)
        await _seed_owner(db, settings)

        adapters = {
            "whatsapp": WhatsAppAdapter(settings),
            "email": EmailAdapter(settings),
            "github": GitHubAdapter(settings),
        }
        llm_client = llm
        if llm_client is None:
            llm_client = OpenAICompatibleLLM(
                base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                model=settings.llm_model, timeout_seconds=settings.llm_timeout_seconds)
        audit = AuditService(db)
        events = EventBus(db)
        approvals = ApprovalService(db, events, audit)
        permissions = PermissionService(db)
        permission_engine = PermissionEngine(permissions.state_for_resource)
        notifications = NotificationService(db, settings, adapters["whatsapp"], audit)
        incidents = IncidentService(db, events, audit, notifications)
        verification_engine = VerificationEngine(db, adapters)

        tasks = TaskService(db, audit, events, verification_engine)
        email = EmailService(db, settings, adapters["email"], permission_engine, approvals, audit)
        gmail = GmailService(db, settings, GmailHTTPAdapter(settings), audit)
        whatsapp = WhatsAppService(db, settings, adapters["whatsapp"], permission_engine, approvals, audit, events)
        github = GitHubService(db, adapters["github"], permission_engine, audit)
        memory = MemoryService(db, audit)
        monitoring = MonitoringService(db, adapters, audit)
        automations = AutomationService(db, audit, events)
        brain = AgentBrain(db, llm_client, settings, adapters)
        agent = AgentService(db, settings, brain, permission_engine, verification_engine,
                             audit, events, approvals, adapters, incidents, tasks, email,
                             whatsapp, github, memory, automations)
        scheduler = Scheduler(db, events, monitoring, tasks, notifications, email, automations)
        system = SystemService(db, adapters, lambda: scheduler.running)
        app.state.llm = llm_client

        services = {
            "auth": AuthService(db, settings, audit),
            "agent": agent,
            "executions": ExecutionService(db),
            "projects": ProjectService(db, audit, events),
            "tasks": tasks,
            "memory": memory,
            "whatsapp": whatsapp,
            "email": email,
            "gmail": gmail,
            "github": github,
            "monitoring": monitoring,
            "automations": automations,
            "approvals": approvals,
            "permissions": permissions,
            "permission_engine": permission_engine,
            "notifications": notifications,
            "audit": audit,
            "incidents": incidents,
            "system": system,
            "brief": BriefService(db),
            "idempotency": IdempotencyService(db),
        }

        app.state.db = db
        app.state.services = services
        app.state.adapters = adapters
        app.state.scheduler = scheduler
        app.state.agent_ok = True

        events.bind_automation_runner(app.state.scheduler.run_event_automations)
        await app.state.scheduler.start()
        try:
            yield
        finally:
            await app.state.scheduler.stop()
            for adapter in adapters.values():
                await adapter.close()
            await gmail.close()
            await llm_client.close()
            await db.close()

    app = FastAPI(
        title="GHAYATH PERSONAL AI — Core API",
        version="1.2.0",
        lifespan=lifespan,
        default_response_class=ZJSONResponse,
    )
    app.state.settings = settings
    app.state.agent_ok = False

    def agent_ready() -> bool:
        return bool(getattr(app.state, "agent_ok", False))

    app.state.agent_ready = agent_ready

    install_exception_handlers(app)
    install_rate_limiter(app)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or context.new_request_id()
        tokens = (
            context.request_id_var.set(rid),
            context.user_id_var.set(None),
            context.role_var.set(None),
            context.actor_var.set(None),
            context.action_var.set(None),
            context.target_var.set(None),
            context.execution_id_var.set(None),
        )
        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            context.log_event("request", {
                "method": request.method, "path": request.url.path, "duration_ms": duration_ms,
            })
            for var, tok in zip((context.request_id_var, context.user_id_var, context.role_var,
                                  context.actor_var, context.action_var, context.target_var,
                                  context.execution_id_var), tokens):
                var.reset(tok)
        response.headers["X-Request-Id"] = rid
        return response

    app.include_router(create_v1_router())

    @app.get("/ready")
    async def ready(request: Request):
        # Ops readiness probe (deliberately outside the versioned /api/v1 surface).
        db_ok = await app.state.db.health()
        migrated = (await app.state.db.fetchone("SELECT to_regclass('public.users') IS NOT NULL AS m"))["m"]
        if db_ok and migrated:
            return {"status": "ready"}
        return JSONResponse({"status": "not_ready", "database": db_ok, "migrated": bool(migrated)}, status_code=503)

    return app


async def _seed_owner(db: DB, settings: Settings) -> None:
    """One-time OWNER bootstrap from environment (idempotent). Never auto-creates others."""
    if not (settings.seed_owner_email and settings.seed_owner_password):
        return
    existing = await db.users.get_by_email(settings.seed_owner_email)
    if existing:
        return
    await db.users.create(new_id("usr"), settings.seed_owner_email,
                          hash_password(settings.seed_owner_password), "OWNER", "Owner")
    context.log_event("owner.seeded", {"email": settings.seed_owner_email})


def _default_app() -> FastAPI | None:
    """Module-level app for `uvicorn app.main:app`. Requires the environment to be
    configured (GHAYATH_AUTH_JWT_SECRET etc.); import must stay safe without it."""
    try:
        return create_app()
    except RuntimeError as e:
        import sys

        print(f"ghayath: cannot create app: {e}", file=sys.stderr)
        return None


app = _default_app()
