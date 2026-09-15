# GHAYATH PERSONAL AI — Database Schema (v1.0)

**قاعدة البيانات:** PostgreSQL 14+
**الملف المنفَّذ:** [`../sql/001_init.sql`](../sql/001_init.sql) (81 عبارة، مُتحقَّق منه على Postgres فعلي)
**العقد المرتبط:** [`openapi.yaml`](../openapi/openapi.yaml)

---

## 1. خريطة الجداول

| # | الجدول | الغرض | المفتاح |
|---|---|---|---|
| 1 | `users` | المستخدمون وأدوار RBAC | `usr_` |
| 2 | `auth_tokens` | Refresh tokens (مخزّنة hashed + rotation) | `tok_` |
| 3 | `conversations` | محادثات الـAgent (استمرارية السياق) | `conv_` |
| 4 | `conversation_messages` | رسائل المحادثة | identity |
| 5 | `projects` | سجل المشاريع | `prj_` |
| 6 | `project_integrations` | ربط مشروع بمورد خارجي (repo/deploy) | مركّب |
| 7 | `tasks` | المهام | `ts_` |
| 8 | `verifications` | سجلات التحقق المستقل (بواب الاكتمال) | `ver_` |
| 9 | `agent_commands` | أوامر الـAgent + النية + الخطة (JSONB) | `cmd_` |
| 10 | `executions` | تنفيذات الخطط | `exec_` |
| 11 | `approvals` | الموافقات البشرية | `appr_` |
| 12 | `automations` | الأتمتة (trigger/action JSONB) + حالة الجدولة | `auto_` |
| 13 | `notifications` | الإشعارات + القنوات التي رُوتيت إليها | `notf_` |
| 14 | `audit_logs` | **السجل غير القابل للتعديل** | identity |
| 15 | `incidents` | الحوادث الطارئة | `inc_` |
| 16 | `integrations` | حالة/أذونات مزوّدي الخدمات (3 صفوف ثابتة) | slug |
| 17 | `whatsapp_messages` | رسائل الواتساب (داخل/خارج) + تصنيف | `wmsg_` |
| 18 | `email_messages` | البريد + تحليل الـAgent | `msg_` |
| 19 | `email_drafts` | المسودات (لا تُرسل إلا عبر `/email/send`) | `draft_` |
| 20 | `github_repositories` | المستودعات المتتبَّعة | `repo_` |
| 21 | `repo_status_snapshots` | تاريخ حالة المستودعات (آخر صف = الحالة) | مركّب |
| 22 | `github_issues` | Issues أنشأها الـAgent | `iss_` |
| 23 | `memory` | الذاكرة الدائمة المهيكلة | `mem_` |
| 24 | `events` | سجل الـEvent Bus الداخلي (append-only) | `evt_` |
| 25 | `idempotency_keys` | أمان الإعادة (24 ساعة) | مركّب |

## 2. اتفاقيات

### 2.1 المعرّفات
- `TEXT` ببادئة مجال + ULID من التطبيق: `usr_01J...`, `prj_sanaa`, `cmd_001`.
- البادئة + النمط (`~ '^prj_[a-z0-9]+$'`) مقيَّدان في DDL.
- الاستثناء الوحيد: `audit_logs.id` — `BIGINT IDENTITY` تسلسلي (طابع ترتيب زمني حقيقي).

### 2.2 الوقت
- كل الأعمدة `TIMESTAMPTZ`، تخزين UTC داخلياً.
- التحويل للمنطقة الزمنية **مسؤولية العميل** (Dashboard)، لا قاعدة البيانات.
- `updated_at` تُدار بترغيب (trigger) موحد — لا يلمسها التطبيق يدوياً.

### 2.3 القوام (Enums)
قوامات `CHECK` بدل `CREATE TYPE` — أيسر للمigrations. القيم مطابقة للعقد:

| الجدول/المجال | القيم |
|---|---|
| users.role | `OWNER, AGENT, VIEWER` |
| projects.status | `ACTIVE, ON_HOLD, PAUSED, COMPLETED, ARCHIVED` |
| priority (عامة) | `P0, P1, P2, P3` |
| tasks.status | `TODO, IN_PROGRESS, BLOCKED, IN_REVIEW, DONE, CANCELLED` |
| verifications.status | `PENDING, PASSED, FAILED, SKIPPED` |
| agent_commands/executions.status | `PLANNED, WAITING_APPROVAL, RUNNING, VERIFYING, COMPLETED, FAILED, BLOCKED, CANCELLED` |
| approvals.type | `AGENT_EXECUTION, EMAIL_SEND, WHATSAPP_SEND, GENERIC` |
| approvals.status | `PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED` |
| notifications.severity | `LOW, MEDIUM, HIGH, CRITICAL` |
| notifications.status | `QUEUED, SENT, FAILED, DISMISSED` |
| audit_logs.result | `SUCCESS, FAILURE, DENIED, TIMEOUT` |
| incidents.status | `OPEN, INVESTIGATING, MITIGATED, RESOLVED, CLOSED` |
| integrations.status | `CONNECTED, DISCONNECTED, DEGRADED, EXPIRED` |
| whatsapp.status | `QUEUED, SENT, DELIVERED, READ, FAILED` |
| classification (واتساب/بريد) | `PERSONAL, BUSINESS, CLIENT, PROJECT, URGENT, SPAM, UNKNOWN` |
| email.priority | `LOW, NORMAL, HIGH, URGENT` |
| email_drafts.status | `DRAFT, APPROVAL_PENDING, SENT, REJECTED, DISCARDED` |
| repo.build / tests / deployment | `PASSING, FAILING, UNKNOWN` / `+ RUNNING` / `HEALTHY, DEGRADED, DOWN, UNKNOWN` |
| memory.type | 9 أنواع من العقد (`USER_PROFILE` ... `AUTOMATION`) |
| memory.source | `agent, user, integration, verified, inference` |
| events.event | 12 حدثاً من عقد الـEvent Bus |

### 2.4 JSONB
الحقول المرنة شكلها **معرّف في OpenAPI** (لا في قاعدة البيانات):
`agent_commands.plan`, `automations.trigger/action`, `verifications.checks`,
`events.payload`, `memory.value`, `integrations.permissions`, `idempotency_keys.response_body`.

## 3. الثوابت المعمارية (مُطبَّقة في DDL)

### 3.1 Audit Log غير قابل للتعديل
طبقتان:
1. **Trigger** يرفض أي `UPDATE`/`DELETE` على `audit_logs` (يُرفع استثناء).
2. **أذونات** (في قسم الأدوار في ملف SQL): `ghayath_app` يملك `SELECT, INSERT` فقط على الجدول — لا `UPDATE/DELETE`.

النتيجة: لا يمكن لأي مسار (Agent أو حتى خطأ في الكود) محو أو تجميل السجل.

### 3.2 Task Completion بمسار تحقق
`tasks.status = DONE` لا يُنصَّل مباشرة (العقد يرفضه في `PATCH /tasks/{id}`).
الدورة: `... → IN_REVIEW` مع `verifications` سجل `PENDING` → `PASSED` → `DONE` + `completed_at`.
الـAgent الذي يدّعي الاكتمال ليس مصدر صلاحيته.

### 3.3 Idempotency
- `idempotency_keys (scope, key)` فريد + `request_hash` — طلب بإعادة بمفتاح مختلف body يرفُض.
- `whatsapp_messages.idempotency_key` فريد جزئياً، و`external_id` فريد (dedupe الوارد).
- `email_messages.external_id` فريد (dedupe Gmail push).

### 3.4 Memory بديلاً عن التكرار
`UNIQUE (type, key, COALESCE(project_id,''))` — نفس المفتاح يحدَّث (upsert) بدل تراكم نسخ متناقضة.
`source = verified` يفصل الحقائق المؤكدة عن استنتاجات الـAgent (`confidence`).

### 3.5 Event Bus داخلي
`events` هو سجل دائم للـBus (append-only عملياً) — مصادر/مستهلكون داخليون (Scheduler, Agent, Notification Router).
**لا يوجد endpoint HTTP للأحداث في v1** — الوصول عبر `/audit`, `/brief/daily`, والإشعارات.

## 4. الأدوار والأذونات (يُنفَّذ وقت الـdeployment)

| الدور | الصلاحيات |
|---|---|
| `ghayath_app` | DML كامل **ما عدا** تعديل `audit_logs` |
| `ghayath_readonly` | `SELECT` فقط (لقطة Dashboard/تقارير) |

مكتوب جاهزاً (معلَّق) في نهاية `001_init.sql`.

## 5. Migrations مستقبلاً
- ابدأ بـ`001_init.sql` (هذا الملف).
- كل تغيير: `002_xxx.sql` — لا تُعدَّل الـ001 بعد الـdeploy.
- القوامات CHECK → تغييرها migration عادي (DROP/ADD CONSTRAINT).

## 6. ملاحظات أداء
- الفلاتر الشائعة كلها مؤشَّرة: `tasks (project_id, status, priority, assignee, due_at)`, `audit_logs (project_id, occurred_at, action, actor, execution_id)`, `events (event, occurred_at)`, `email_messages (folder, received_at)`.
- `repo_status_snapshots` يقدَّم بآخر صف لكل repo (استعلام `DISTINCT ON` بسيط).
- `events` و`audit_logs` سيقبلان نمواً كبيراً — خطّط partitioning حسب الشهر في v1.x إن لزم (لا يغيّر العقد).
