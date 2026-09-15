# GHAYATH FastAPI Core — Build Verification Report (Phase 20)

Date: 2026-09-15 · Branch: `arena/01a0a2cf-mg-labs` · Test DB: real PostgreSQL 16.2 (embedded `pgserver`)

Final verification run: **125/125 tests passed** (`pytest tests/`, ~5 min).

```
GHAYATH FASTAPI CORE

Implementation:        PASS — 34 of 40 spec operations implemented (6 deferred per contract:
                       getExecution, getEventsStream, listAutomations, deleteAutomation,
                       patchIncident, getNotifications). 63 app modules, all compile clean,
                       app boots against real PostgreSQL 16.2; 40/40 v1 routes materialized
                       and matched to contract paths/methods/operationIds.

OpenAPI:               PASS — openapi.yaml validates (openapi-spec-validator, 3.1); all
                       $refs resolve; 40 unique operationIds; fixed 15-error-code set;
                       request/response schemas of every implemented operation verified
                       field-compatible against the generated models (drift test);
                       security level per operation verified (PUBLIC/AUTH/WEBHOOK);
                       documented success status codes verified (201/202/200 per spec).

Database:              PASS — 001_init.sql (unchanged, authoritative) applied cleanly to a
                       real PG 16.2 (25 tables); re-apply is a no-op (idempotent); unique
                       constraints (wamid, idempotency scope+key), FK + CHECK constraints,
                       and append-only audit trigger all verified by executed tests.

Authentication:        PASS — login/refresh/logout; JWT HS256; tampered signature, wrong
                       secret, expired access token, and expired refresh token all
                       rejected with 401 + envelope; logout revokes the refresh family AND
                       the access token (jti revocation verified: token unusable after
                       logout); unknown user → 401, no stack trace.

RBAC:                  PASS — 3 roles only. VIEWER: 8 mutating surfaces all 403
                       PERMISSION_DENIED, reads 200. OWNER: full. AGENT: bound by stored
                       integration bits (all false → deny/approval-gated); AGENT approve
                       endpoint 403; no permission-grant path exists (PUT/PATCH 404/405);
                       fabricated approval_id → 404/409 with zero executions.

Permissions:           PASS — GET /permissions returns the three integration groups;
                       POST /permissions/check: OWNER allowed, AGENT allowed=false +
                       requires_approval=true; engine has no grant path; agent cannot
                       grant itself or bypass gates (executions table stays empty).

Approval Engine:       PASS — 409 + error.details.approval_id before any side effect
                       (provider call count asserted 0 pre-approval); identical resubmit
                       executes exactly once; OWNER-only decision; re-decision 409
                       CONFLICT; reject flow works; 30-min TTL (scheduler expires
                       PENDING); high-impact email content forces approval even for OWNER;
                       WhatsApp send (no approval field in contract) resubmits via
                       exact-payload grant match.

Idempotency:           PASS — mandatory (400 without key) on whatsapp send, email send,
                       agent execute, task complete, incident create; same key+body
                       replays the stored result with provider called exactly once and
                       exactly 1 DB row; same key + different body → 409 CONFLICT.

Rate Limiting:         PASS — /auth/* 5/min (6th → 429 RATE_LIMITED + X-RateLimit-Reset),
                       /agent/{command,execute} 60/min (61st → 429); unlisted endpoints
                       not limited; no undocumented limits.

Webhook HMAC:          PASS — HMAC-SHA256 over raw body; missing/wrong/wrong-secret/
                       tampered signatures → 401 and row NOT written (unverified payloads
                       never processed); contract required fields enforced (400);
                       allow-list enforced (401 sender_not_allowed); no secret → 503;
                       wamid replay deduped (1 row, 1 event).

Audit Immutability:    PASS — UPDATE/DELETE on audit_logs raise via DB trigger (tested on
                       real PG); log grows monotonically with important actions;
                       request_id/action/target/result recorded.

Health:                PASS — GET /api/v1/health real dependency checks (database
                       healthy, scheduler healthy, redis honestly "down" when absent →
                       overall "degraded", never faked green); GET /ready ops probe
                       (deliberately outside /api/v1) returns ready against live DB.

Agent LLM (rule #30):  PASS — real OpenAI-compatible chat-completions client
                       (app/integrations/llm.py) is the model boundary; the brain
                       (app/agent/brain.py) plans from LIVE context (projects,
                       adapter health, memory, conversation history) and its plan is
                       VALIDATED against the 17-tool registry (unknown tool / bad args /
                       >10 steps -> 400, nothing persisted). No LLM key -> 503
                       INTEGRATION_OFFLINE (never a silent rule-based fallback); provider
                       down at planning -> 503; provider down at summarization ->
                       deterministic template from REAL step results. User text is always
                       wrapped in <user_command> data markers. 14 dedicated tests at the
                       HTTP transport boundary (tests/mock_llm.py, deterministic double):
                       task really created, whatsapp really sent (1 provider call),
                       memory saved with source='agent', AGENT role still gated by
                       approval + permission, RESEARCH_FETCH honestly TOOL_UNAVAILABLE,
                       conversation continuity feeds the next plan prompt.

Contract Tests:        PASS — automated drift test (FastAPI routes ↔ openapi.yaml):
                       missing endpoint, undocumented endpoint, wrong method, wrong
                       operationId, incompatible request/response schema, missing security,
                       wrong documented status — all checked, all green.

Integration Tests:     PASS — 125/125 executed and passing (pytest, real PostgreSQL 16.2,
                       providers + LLM mocked only at the httpx transport boundary): full
                       API contract incl. list filters + pagination, approval flows,
                       LLM-driven agent pipeline (COMPLETED with independent verification;
                       TOOL_UNAVAILABLE / RESOURCE_NOT_FOUND outcomes honest, never faked
                       success; IN_REVIEW task gate), events→automation trigger firing on
                       webhook, scheduler SCHEDULE run path, structured logging with
                       redaction (password, JWT, webhook secret, bearer tokens absent).

Docker:                FAIL (BLOCKED) — Dockerfile, docker-compose.yml (db+redis+app,
                       env-only secrets, .env.example, no .env) are written and reviewed,
                       but this sandbox has no docker daemon/binary, so image build and
                       compose up could NOT be executed. Not claimed as verified.

Remaining Blockers:
  1. Docker build/run unverified (no daemon in sandbox) — run
     `docker compose up --build` on a machine with Docker to close.
  2. Live provider credentials (WhatsApp/Email/GitHub) intentionally absent — provider
     paths verified only via injected transport mocks + explicit INTEGRATION_OFFLINE
     states; a smoke run with real credentials is the remaining end-to-end step.
     Same for the LLM: set GHAYATH_LLM_API_KEY (+ optional GHAYATH_LLM_BASE_URL /
     GHAYATH_LLM_MODEL for any OpenAI-compatible endpoint) for a live-model smoke run;
     until then /agent/command returns 503 INTEGRATION_OFFLINE by design.
  3. Redis optional at runtime (falls back to in-memory rate-limit store; health reports
     it down) — set GHAYATH_REDIS_URL in compose for production.
```

## How to run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# tests (embedded PostgreSQL 16 via pgserver — no system Postgres needed)
python -m pytest tests/ -q

# run the API
export $(grep -v '^#' .env.example | grep '=' | cut -d= -f1 | sed 's/$/=x/')  # placeholders
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Defect log (found and fixed by the executed test suite)

These were real defects caught at runtime — listed for transparency:

1. psycopg3: `Connection` has no `fetch*` — cursor-based access + `dict_row` factory.
2. `JSONResponse(status, content=…)` argument order (webhook + idempotency replay).
3. Generated `SecretStr` password field leaking into `bcrypt`.
4. Generated `RootModel` ID fields (E164, ProjectId, ConversationId, ApprovalId) leaking
   into SQL parameters and idempotency hashes — unwrapped at the router boundary.
5. Generated enum fields with str defaults (`status: TODO`) — `.value` guard at routers.
6. `audit_logs.occurred_at` surfaced as contract `timestamp`; audit list was unmapped.
7. Task/EmailDraft response shapes: `verification` object, `to` field (DB `to_addr`).
8. Sent email: FK `email_drafts.sent_message_id → email_messages` — a real `sent` row is
   now inserted.
9. WhatsApp inbound id violated the `wmsg_[a-z0-9]+` pattern — Crockford ULID ids.
10. cron `next_run_at` stored as float — now `timestamptz`.
11. Login with a wrong password < 8 chars hit the contract `minLength: 8` first (400,
    not 401) — tests use contract-valid passwords.
12. Logout did not invalidate the access token — jti revocation added and verified.
13. Approval resubmit used a different payload than the one requested (exact-payload
    JSONB match failed) — aligned.
14. `GET /incidents/{id}` asserted by a test but absent from the contract — the 404 is the
    correct behavior; test corrected.
15. `await` precedence: `return await self.fetch(...)[::-1]` parses as
    `await (self.fetch(...)[::-1])` — a coroutine is not subscriptable. (conversation
    history for the agent's context)
16. `tool_unavailable(tool, details=...)` called with a `details` kwarg the helper does
    not accept — latent TypeError on the RESEARCH_FETCH / missing-repo paths (the old
    code carried the same latent call); now constructed correctly.
17. `validation_error(details, message)` argument order inverted in the brain's
    plan-rejection path — details/message swapped (details came back as a string).
18. `GitHubAdapter.verify()` only understood create_issue results; repo_status results
    (no `number` field) were always marked failed — verify is now action-aware.
