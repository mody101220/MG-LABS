# GHAYATH PERSONAL AI — Core (Spec v1.0)

مواصفة العقد الرسمية لـ **GHAYATH PERSONAL AI** — Autonomous Operations Agent
ببنية Modular Agentic Architecture، بحيث كل جزء قابل للتحويل مباشرة إلى
FastAPI/Node.js **بدون إعادة تفسير التصميم**.

## الملفات

| الملف | الوصف |
|---|---|
| [`docs/api-contract-specification-v1.0.md`](docs/api-contract-specification-v1.0.md) | العقد الكامل بالعربية (OpenAPI v1.1.0، 46 Endpoint + قواعد عامة) |
| [`openapi/openapi.yaml`](openapi/openapi.yaml) | **OpenAPI 3.1** — كل الـEndpoints، الـSchemas، الأخطاء، Idempotency، Rate Limits |
| [`docs/database-schema.md`](docs/database-schema.md) | توثيق مخطط PostgreSQL (25 جدولاً + الثوابت المعمارية) |
| [`sql/001_init.sql`](sql/001_init.sql) | DDL كامل منفَّذ (PostgreSQL 14+) مع triggers والأدوار |
| [`app/`](app/) | **التنفيذ الفعلي FastAPI** — 46 عملية من عقد OpenAPI v1.1.0، services + repos + agent pipeline |
| [`tests/`](tests/) | **132 اختباراً منفَّذاً** — contract drift، v1.1 RBAC/SSE/transitions، approvals، idempotency، rate limiting، webhook HMAC، immutability، health |
| [`docs/build-report.md`](docs/build-report.md) | **تقرير التحقق النهائي (P20)** — حالة كل بند بالـPASS/FAIL الفعلي + سجل العيوب المكتشفة والمُصلَّحة |
| [`Dockerfile`](Dockerfile) · [`docker-compose.yml`](docker-compose.yml) · [`.env.example`](.env.example) | تجميع وتشغيل (PostgreSQL + Redis + FastAPI) — بلا أسرار في الكود، secrets من البيئة فقط |

## حالة التحقق (2026-09-15)

| الفحص | الأداة | النتيجة |
|---|---|---|
| صياغة OpenAPI 3.1 + كل الـ$refs | `openapi-spec-validator` | ✅ صالح — 46 عملية، 139 schema |
| صياغة SQL (قواعد PostgreSQL الفعلية) | `pglast` (libpg_query) | ✅ 81 عبارة |
| FKs / indexes / triggers دلالياً | فحص AST | ✅ 18 FK، 39 index، 12 trigger |
| **تنفيذ DDL على Postgres فعلي** | PGlite (Postgres/WASM) | ✅ 25 جدولاً + اختبارات: immutability الـaudit، CHECK enums، upsert الـmemory، dedupe الوارد، `updated_at` |

## حالة التنفيذ الفعلي (FastAPI — 2026-09-16)

| البند | النتيجة |
|---|---|
| عمليات العقد المنفَّذة | **46/46** من OpenAPI v1.1.0 (40 عملية v1 محفوظة + العمليات الست المعتمدة) |
| نماذج Pydantic | مولَّدة آلياً من `openapi.yaml` (`app/generated/models.py`) — لا تعريف يدوي لما يمكن توليده |
| **الوكيل (الـAgent Brain)** | **LLM-driven** عبر عميل OpenAI-compatible حقيقي (`app/integrations/llm.py`) — التخطيط/التلخيص بالنموذج، والتحقق من الخطة عبر Tool Registry + Permission Engine. **بلا مفتاح LLM = `INTEGRATION_OFFLINE` بصدق** (لا fallback خفي). 17 أداة (READ/CREATE/SEND/MEMORY/AUTOMATION) |
| الاختبارات | **132/132 ناجحة** على PostgreSQL 16.2 حقيقي (`python -m pytest tests/`) — تشمل اختبارات v1.1 للـRBAC/SSE/transition/idempotency/archive |
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

## v1.1.0 — العمليات الست المضافة

تم اعتماد المقترح وإدماجه في العقد الرسمي `openapi/openapi.yaml`، مع الحفاظ على
عمليات v1 الأربعين دون breaking changes. الإصدار الرسمي يضم 46 عملية:

- `GET /agent/executions/{execution_id}` — حالة التنفيذ من PostgreSQL مع تقييد نتيجة VIEWER.
- `GET /events/stream` — SSE حقيقي من جدول `events` مع cursor وheartbeat وRBAC.
- `GET /automations` — السجلات الموجودة فعلياً مع فلتر `enabled`.
- `DELETE /automations/{id}` — OWNER فقط، hard delete، idempotency وتدقيق immutable.
- `PATCH /incidents/{id}` — مصفوفة انتقالات DDL مع idempotency وتدقيق الملاحظة.
- `GET /notifications` — أرشيف PostgreSQL مع الفلاتر و`limit` الموثق.

سجل الاعتماد التفصيلي: [`docs/api-contract-v1.1-proposal.md`](docs/api-contract-v1.1-proposal.md)
والقطعة الأصلية القابلة للدمج: [`openapi/v1.1-proposal-fragment.yaml`](openapi/v1.1-proposal-fragment.yaml).
العقد الملزم الآن هو OpenAPI الرسمي فقط.

## توليد النماذج (مُعيَّر — لا تعديل يدوي)

`app/generated/models.py` يُولَّد آلياً من `openapi/openapi.yaml` بمولّد **محدد الإصدار**
(0.37.0 مثبت في `pyproject.toml`):

```bash
pip install -e '.[dev]'
python scripts/generate_models.py   # يعيد التوليد + تطبيع الـtimestamp + type-ignores تلقائية
```

CI يفشل إن لم يكن الملف المتولَّد مطابقاً للعقد الحالي (خطوة drift-check).

## التحويل إلى FastAPI (مثال)


```python
# كل response schema في openapi.yaml يتحول 1:1 إلى Pydantic عبر fastapi -> openapi
pip install fastapi uvicorn
# توليد الـschemas:
python scripts/generate_models.py   # انظر قسم «توليد النماذج» أعلاه
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
