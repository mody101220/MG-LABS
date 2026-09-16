# GHAYATH PERSONAL AI — Core (Spec v1.2)

مواصفة العقد الرسمية لـ **GHAYATH PERSONAL AI** — Autonomous Operations Agent
ببنية Modular Agentic Architecture، بحيث كل جزء قابل للتحويل مباشرة إلى
FastAPI/Node.js **بدون إعادة تفسير التصميم**.

## الملفات

| الملف | الوصف |
|---|---|
| [`docs/api-contract-specification-v1.0.md`](docs/api-contract-specification-v1.0.md) | العقد الأساسي المحفوظ؛ العقد الملزم الحالي هو OpenAPI v1.2.0 |
| [`openapi/openapi.yaml`](openapi/openapi.yaml) | **OpenAPI 3.1 v1.2.0** — 54 عملية، 159 schema، Gmail OAuth/read/sync/revoke |
| [`docs/gmail-integration.md`](docs/gmail-integration.md) | إعداد Google Cloud، redirect URI، readonly scope، الأسرار، حالات الاتصال والفشل |
| [`docs/database-schema.md`](docs/database-schema.md) | توثيق مخطط PostgreSQL الأساسي والإضافي لـ Gmail (27 جدولاً بعد migration 002) |
| [`sql/001_init.sql`](sql/001_init.sql) · [`sql/002_gmail_oauth.sql`](sql/002_gmail_oauth.sql) | DDL الأساسي + migration إضافي صريح لـ OAuth state وGmail message mapping |
| [`app/`](app/) | **التنفيذ الفعلي FastAPI** — 54 عملية من عقد OpenAPI v1.2.0، Gmail adapter/service + repos + agent pipeline |
| [`tests/`](tests/) | اختبارات contract drift، PostgreSQL، RBAC/approvals/idempotency، وGmail OAuth/normalization/retry/dedupe/unavailable states |
| [`docs/build-report.md`](docs/build-report.md) | **تقرير التحقق النهائي (P20)** — حالة كل بند بالـPASS/FAIL الفعلي + سجل العيوب المكتشفة والمُصلَّحة |
| [`Dockerfile`](Dockerfile) · [`docker-compose.yml`](docker-compose.yml) · [`.env.example`](.env.example) | تجميع وتشغيل (PostgreSQL + Redis + FastAPI) — بلا أسرار في الكود، secrets من البيئة فقط |

## حالة التحقق (2026-09-16)

| الفحص | الأداة | النتيجة |
|---|---|---|
| صياغة OpenAPI 3.1 + كل الـ$refs | `openapi-spec-validator` | ✅ صالح — 54 عملية، 159 schema |
| SQL/migrations على PostgreSQL فعلي | `pytest` + `pgserver` | ✅ `001_init.sql` + `002_gmail_oauth.sql`، migration idempotent، 27 جدولاً، Gmail mapping upsert |
| FKs / indexes / triggers | اختبارات PostgreSQL الحالية | ✅ base constraints، audit immutability، Gmail state/message keys |
| **تنفيذ DDL على Postgres فعلي** | PostgreSQL 16.2 عبر `pgserver` | ✅ اختبارات immutability، CHECK/FK، upsert/dedupe، Gmail messageId/threadId mapping |

## حالة التنفيذ الفعلي (FastAPI — 2026-09-16)

| البند | النتيجة |
|---|---|
| عمليات العقد المنفَّذة | **54/54** من OpenAPI v1.2.0 (عمليات v1.1 محفوظة + عمليات Gmail الموثقة) |
| نماذج Pydantic | مولَّدة آلياً من `openapi.yaml` (`app/generated/models.py`) — لا تعريف يدوي لما يمكن توليده |
| **الوكيل (الـAgent Brain)** | **LLM-driven** عبر عميل OpenAI-compatible حقيقي (`app/integrations/llm.py`) — التخطيط/التلخيص بالنموذج، والتحقق من الخطة عبر Tool Registry + Permission Engine. **بلا مفتاح LLM = `INTEGRATION_OFFLINE` بصدق** (لا fallback خفي). 17 أداة (READ/CREATE/SEND/MEMORY/AUTOMATION) |
| الاختبارات | **140 passed** على PostgreSQL 16.2 حقيقي (`PYTHONPATH=... pytest -q tests`)؛ تشمل Gmail OAuth/state/retry/normalization/thread identity/dedupe/unavailable states؛ لا يوجد اتصال Google حقيقي في هذه البيئة |
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

## Gmail v1.2.0 — تكامل القراءة الحقيقي

أضيف Gmail كـadapter مستقل مع OAuth server-side، حالة CSRF one-time، تخزين
Fernet مشفّر، refresh/revoke، pagination من Google، جلب التفاصيل بعد list،
normalization، thread/message IDs، وsync idempotent إلى unified inbox. النطاق
الوحيد في هذه المرحلة هو `https://www.googleapis.com/auth/gmail.readonly`؛ لا يوجد
send/reply وهمي. عند غياب الاتصال تكون الحالة `REQUIRES_CONNECTION` ولا يعاد
inbox فارغ على أنه نجاح. الهوية غير المثبتة تبقى `IDENTITY_UNVERIFIED` أو
`UNKNOWN` ولا تُستنتج من تشابه الاسم. الإعداد الكامل في
[`docs/gmail-integration.md`](docs/gmail-integration.md)، واختبار Google الحقيقي
محجوب حالياً لغياب credentials.

## v1.1.0 — العمليات الست المضافة

تم اعتماد المقترح تاريخياً وإدماجه في `openapi/openapi.yaml`، مع الحفاظ على
عمليات v1 الأربعين دون breaking changes. الإصدار الحالي v1.2 يضم هذه العمليات
بالإضافة إلى عمليات Gmail الموثقة (54 عملية إجمالاً):

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
