# GHAYATH PERSONAL AI — Core (Spec v1.0)

مواصفة العقد الرسمية لـ **GHAYATH PERSONAL AI** — Autonomous Operations Agent
ببنية Modular Agentic Architecture، بحيث كل جزء قابل للتحويل مباشرة إلى
FastAPI/Node.js **بدون إعادة تفسير التصميم**.

## الملفات

| الملف | الوصف |
|---|---|
| [`docs/api-contract-specification-v1.0.md`](docs/api-contract-specification-v1.0.md) | العقد الكامل بالعربية (40 Endpoint + قواعد عامة) |
| [`openapi/openapi.yaml`](openapi/openapi.yaml) | **OpenAPI 3.1** — كل الـEndpoints، الـSchemas، الأخطاء، Idempotency، Rate Limits |
| [`docs/database-schema.md`](docs/database-schema.md) | توثيق مخطط PostgreSQL (25 جدولاً + الثوابت المعمارية) |
| [`sql/001_init.sql`](sql/001_init.sql) | DDL كامل منفَّذ (PostgreSQL 14+) مع triggers والأدوار |
| [`app/`](app/) | **التنفيذ الفعلي FastAPI** — 34 عملية من العقد (6 مؤجَّلة لـv1.1)، services + repos + agent pipeline |
| [`tests/`](tests/) | **125 اختباراً منفَّذاً** — contract drift، RBAC، approvals، idempotency، rate limiting، webhook HMAC، immutability، health |
| [`docs/build-report.md`](docs/build-report.md) | **تقرير التحقق النهائي (P20)** — حالة كل بند بالـPASS/FAIL الفعلي + سجل العيوب المكتشفة والمُصلَّحة |
| [`Dockerfile`](Dockerfile) · [`docker-compose.yml`](docker-compose.yml) · [`.env.example`](.env.example) | تجميع وتشغيل (PostgreSQL + Redis + FastAPI) — بلا أسرار في الكود، secrets من البيئة فقط |

## حالة التحقق (2026-09-15)

| الفحص | الأداة | النتيجة |
|---|---|---|
| صياغة OpenAPI 3.1 + كل الـ$refs | `openapi-spec-validator` | ✅ صالح — 40 عملية، 128 schema |
| صياغة SQL (قواعد PostgreSQL الفعلية) | `pglast` (libpg_query) | ✅ 81 عبارة |
| FKs / indexes / triggers دلالياً | فحص AST | ✅ 18 FK، 39 index، 12 trigger |
| **تنفيذ DDL على Postgres فعلي** | PGlite (Postgres/WASM) | ✅ 25 جدولاً + اختبارات: immutability الـaudit، CHECK enums، upsert الـmemory، dedupe الوارد، `updated_at` |

## حالة التنفيذ الفعلي (FastAPI — 2026-09-15)

| البند | النتيجة |
|---|---|
| عمليات العقد المنفَّذة | 34/40 (الـ6 مؤجَّلة حسب العقد: getExecution, getEventsStream, listAutomations, deleteAutomation, patchIncident, getNotifications) |
| نماذج Pydantic | مولَّدة آلياً من `openapi.yaml` (`app/generated/models.py`) — لا تعريف يدوي لما يمكن توليده |
| **الوكيل (الـAgent Brain)** | **LLM-driven** عبر عميل OpenAI-compatible حقيقي (`app/integrations/llm.py`) — التخطيط/التلخيص بالنموذج، والتحقق من الخطة عبر Tool Registry + Permission Engine. **بلا مفتاح LLM = `INTEGRATION_OFFLINE` بصدق** (لا fallback خفي). 17 أداة (READ/CREATE/SEND/MEMORY/AUTOMATION) |
| الاختبارات | **125/125 ناجحة** على PostgreSQL 16.2 حقيقي (`pytest tests/`) — من ضمنها 14 اختباراً مخصصاً لطبقة الوكيل/النموذج — تفاصيل كاملة في [`docs/build-report.md`](docs/build-report.md) |
| Docker | Dockerfile + compose مكتوبان ومراجَآن؛ **البناء غير مُتحقق منه** (لا يوجد Docker daemon في بيئة التطوير) |

```bash
pip install -e '.[dev]'   # داخل venv
python -m pytest tests/ -q   # postgres مضمَّن عبر pgserver — لا يحتاج تثبيتاً نظامياً

# تشغيل الـAPI مع الوكيل (النموذج إلزامي لتخطيط الأوامر):
export GHAYATH_LLM_API_KEY=sk-...      # أي endpoint متوافق مع OpenAI
export GHAYATH_LLM_BASE_URL=https://api.openai.com/v1
export GHAYATH_LLM_MODEL=gpt-4o-mini
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## قرارات مصمَّمة (ثابتة في العقد)

1. **Envelope واحد** لكل الاستجابات: `{success, data|error, request_id, timestamp}`.
2. **الأخطاء مجموعة ثابتة** من 15 رمزاً مapped على HTTP codes — لا رموز جديدة دون Revision.
3. **Idempotency-Key إلزامي** على العمليات ذات الأثر الخارجي:
   `/whatsapp/messages/send`, `/email/send`, `/agent/execute`, `/tasks/{id}/complete`, `/incidents`.
4. **APPROVAL_REQUIRED = 409** مع `error.details.approval_id` — العميل يقدّم الـid عند الإعادة.
5. **`PATCH /tasks/{id}` يرفض `DONE`** — الاكتمال فقط عبر `POST /tasks/{id}/complete` (بواب تحقق).
6. **Webhook الواتساب** موقّع بـ `X-Webhook-Signature` (HMAC-SHA256 على raw body) —
   الـsignature secret مخزَّن في `integrations.webhook_secret`.
7. **RBAC أدوار:** `OWNER` (يقرر كل شيء)، `AGENT` (مبدأ خدمي داخلي — لا يمنح نفسه أذونات)،
   `VIEWER` (قراءة فقط للـDashboard).
8. **التصنيفات** (PERSONAL/BUSINESS/CLIENT/PROJECT/URGENT/SPAM/UNKNOWN) موحَّدة
   بين الواتساب والبريد.
9. **الأحداث**: Bus داخلي + سجل `events` — **لا endpoint HTTP** في v1 (قيد ملاحظ أدناه).
10. **`GET /health` عام** (بدون auth) و`/auth/login` غير محمي؛ كل ما عداه Bearer.

## مرشحات v1.1 (مقصود إقصاؤها من v1 — أضيفت هنا بوضوح)

| المرشح | السبب |
|---|---|
| `GET /agent/executions/{execution_id}` | متابعة حالة التنفيذ مباشرة (حالياً عبر events/notifications/audit) |
| `GET /events/stream` (SSE) | Realtime للـDashboard |
| `GET /automations` + `DELETE /automations/{id}` | إدارة الأتمتة القائمة |
| `PATCH /incidents/{id}` (حالة الحادث) | إغلاق/تتبع الحوادث |
| `GET /notifications` | أرشيف الإشعارات |

كل مرشح أعلاه يضاف بـRevision للعقد (رقم version) — لا يُضاف ضمناً.

## التحويل إلى FastAPI (مثال)

```python
# كل response schema في openapi.yaml يتحول 1:1 إلى Pydantic عبر fastapi -> openapi
pip install fastapi uvicorn
# توليد الـschemas:
datamodel-codegen --input openapi.yaml --output schemas.py
# الـenvelope: wrapper واحد
def ok(data, request_id, ts): return {"success": True, "data": data, "request_id": request_id, "timestamp": ts}
```

وإلى Node.js عبر `openapi-typescript` ثم `zod`/`valibot` للـruntime validation.

## MVP (من العقد — الملحق A)

```
CORE AGENT
     │
     ├── PostgreSQL   ← هذا المخطط
     ├── Redis        ← cache + rate limiting + idempotency hot layer
     ├── Memory / Tasks / Projects / Permissions / Approvals / Audit / Scheduler
             │
             ├── Gmail
             ├── WhatsApp
             └── GitHub
```
