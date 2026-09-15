# GHAYATH FASTAPI CORE — Implementation Map

Internal map produced in Phase 1 and extended for v1.1. Source of truth:
`openapi.yaml` (46 operations, 139 schemas), `001_init.sql` (25 tables), contract v1.1.0.

## Operation → handler mapping (exact operationIds)

| Domain | operations (operationId) | service |
|---|---|---|
| Auth | `login` `refreshToken` `logout` | AuthService |
| Agent | `agentCommand` `agentExecute` `getExecution` | AgentService + ExecutionService (IntentEngine→Planner→ToolRouter→PermissionEngine→Executor→Verification→Audit) |
| Events | `getEventsStream` | Events router + EventRepository (authenticated PostgreSQL-backed SSE) |
| Projects | `listProjects` `createProject` `getProject` `updateProject` | ProjectService |
| Tasks | `createTask` `listTasks` `updateTask` `completeTask` | TaskService + VerificationEngine |
| Memory | `createMemory` `listMemory` `deleteMemory` | MemoryService |
| WhatsApp | `getWhatsappStatus` `sendWhatsappMessage` | WhatsAppService (adapter) |
| Webhooks | `whatsappWebhook` | WebhookService (HMAC + dedupe) |
| Email | `listEmailMessages` `getEmailMessage` `createEmailDraft` `sendEmail` | EmailService (adapter) |
| GitHub | `listGitHubRepositories` `getRepoStatus` `createGitHubIssue` | GitHubService (adapter) |
| Monitoring | `monitoringCheck` | MonitoringService (adapters) |
| Automations | `createAutomation` `updateAutomation` `listAutomations` `deleteAutomation` | AutomationService + Scheduler |
| Approvals | `listApprovals` `approveApproval` `rejectApproval` | ApprovalService |
| Permissions | `getPermissions` `checkPermission` | PermissionEngine |
| Notifications | `createNotification` `getNotifications` | NotificationService (severity routing + persisted archive) |
| Audit | `listAudit` | AuditService (INSERT-only) |
| Incidents | `createIncident` `patchIncident` | IncidentService (DDL transition matrix + audit) |
| System | `health` `systemStatus` | SystemService (real dependency probes) |
| Brief | `getDailyBrief` | BriefService (DB-derived) |

**Deferred (v1.1 — NOT implemented):** `GET /agent/executions/{id}`, `GET /events/stream`,
`GET /automations`, `DELETE /automations/{id}`, `PATCH /incidents/{id}`, `GET /notifications`.

## Security model (per operation)

- Public (`security: []`): `login`, `refreshToken`, `health`.
- Webhook-signed: `whatsappWebhook` (X-Webhook-Signature HMAC-SHA256; sender allow-list).
- Everything else: Bearer JWT + role/permission matrix:
  - `VIEWER`: read-only (GET) — mutating ops → 403 PERMISSION_DENIED.
  - `AGENT`: allowed only if the stored permission bit is true; if false and the action is
    approvable (SEND/EXECUTE family) → approval created + 409 APPROVAL_REQUIRED (details.approval_id);
    else → 403. AGENT never self-grants (no mutation path for permissions exists in v1 contract).
  - `OWNER`: allowed on all operations in the contract.

## Side-effect ops requiring Idempotency-Key (mandatory header)

`sendWhatsappMessage`, `sendEmail`, `agentExecute`, `completeTask`, `createIncident`
→ scope table `idempotency_keys (scope, key, request_hash, response_code, response_body)`.

## Rate limits (exactly as contracted)

- `/auth/*` → 5/min
- `/agent/command`, `/agent/execute` → 60/min
- WhatsApp/Email: provider-dependent (not gateway-enforced here)
- Monitoring: scheduler-paced (not gateway-enforced here)
- 429 body = envelope with `RATE_LIMITED` + `X-RateLimit-Reset` header.

## Pipeline (every side effect)

AUTH → ROLE → PERMISSION CHECK → RISK/APPROVAL CHECK → IDEMPOTENCY → EXECUTION (adapter)
→ VERIFICATION → AUDIT (INSERT only). LLM/agent layer never touches adapter directly —
only via ToolRouter.

## Envelope & errors

Success: `{success:true, data, request_id, timestamp}`.
Error: `{success:false, error:{code,message,details}, request_id, timestamp}` with the fixed
15-code set and its HTTP mapping. `request_id` from `X-Request-Id` (client) or generated.

## DB access

psycopg3 (async pool) + raw parameterized SQL in repositories. DDL is authoritative —
no ORM models, no auto-create. Repositories: users/tokens, projects, tasks, verifications,
commands/executions, approvals, automations, notifications, audit, incidents,
integrations, whatsapp, email, github, memory, events, idempotency, conversations.
