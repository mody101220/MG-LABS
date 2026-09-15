# GHIYATH — API Contract v1.1 — مقترح Revision (PROPOSAL — غير مُعتمد)

**حالة:** مقترح للقرار. عقد v1 (`openapi/openapi.yaml`) **لم يُعدَّل ولا يُعدَّل** حتى
اعتماد هذا المقترح. هذا الملف يقدّم التعريف الكامل للعمليات الست المؤجَّلة في
`README.md` (قسم "مرشحات v1.1") بصيغة قابلة للتنفيذ المباشرة: schemas + status codes +
صلاحيات + قواعد approval/idempotency/audit.

**نوع التغيير:** إضافي بالكامل (additive / non-breaking) — لا تغيّر في أي operationId
أو path أو schema موجود في v1. `info.version`: `1.0.x → 1.1.0`.

**قاعدة التصميم المعتمدة هنا (نفس قواعد v1):**
- المصادقة: JWT (global security) + `register(method, path, "AUTH")` لكل مسار جديد
  (يصبح العدد 46 بدل 40)، والتحقق من الدور في طبقة الخدمة (نموذج الأدوار الثلاثة
  OWNER/AGENT/VIEWER دون أدوار إضافية).
- التغليف: `{success, data|error{code,message,details}, request_id, timestamp}`.
- أكواد الخطأ: نفس **15** الرمز الثابت — لا رمز جديد (الاستثناءات أدناه تستخدم
  `CONFLICT` الموجود أصلًا).
- Rate limiting: **بدون أي تiers جديدة** — عقد §28 كما هو (auth 5/دقيقة، agent
  60/دقيقة، والبقية بلا حد — "لا حدود غير موثقة").
- Approval: قائمة العمليات القابلة للموافقة (`_APPROVAL_ELIGIBLE`) **غير مغيّرة** —
  العمليات الست الجديدة ليست أدوات حساسة بل عمليات تتبع/إدارة سجلات، لذلك لا تمر
  بموافقة بشرية (مُسجَّل هنا كقرار صريح).
- DDL: **صفر** تغييرات — كل العمليات الست تخدم من جداول `001_init.sql` الحالية
  (التفاصيل تحت كل عملية). لا migration جديد.

---

## 1. `getExecution` — متابعة حالة تنفيذ أمر الوكيل

```
GET /agent/executions/{execution_id}
operationId: getExecution
```

**الهدف:** استعلام مباشر عن حالة تنفيذ (بدل الاستدلال من events/notifications/audit).

**البيانات المصدرية (بدون DDL جديد):** جدول `executions`
(id, command_id, approval_id, status, started_at, finished_at, error_code,
error_message, verification_id, result, created_at) + خطوات الخطة من `agent_commands.plan`
+ حالة الخطوات من `executions.result`.

**الصلاحيات:** OWNER + AGENT + VIEWER (قراءة — نفس وضعية قراءة `/audit` في v1).
**Rate limit:** بلا tier (حسب العقد).
**Idempotency:** غير مطلوب (GET).

**Response 200:**
```json
{
  "success": true,
  "data": {
    "execution": {
      "id": "exec_01…", "command_id": "cmd_01…", "approval_id": null,
      "status": "VERIFYING", "started_at": "…", "finished_at": null,
      "error_code": null, "error_message": null,
      "verification_id": null, "created_at": "…"
    },
    "steps": [
      {"index": 0, "tool": "github.create_issue", "status": "SUCCEEDED",
       "error": null, "output_summary": "issue #142 created (…≤500 حرف)"}
    ],
    "result": { "…": "كامل نتيجة التنفيذ JSON" }
  },
  "request_id": "req_…", "timestamp": "…"
}
```

**قاعدة تقييد حساسة (قرار صريح):** حقل `result` الكامل يُرسل لـ **OWNER وAGENT فقط**؛
لـ **VIEWER** يُستبدل بـ `result: null` وتبقى `steps[].output_summary` (مقتطف ≤500
حرف). السبب: نتائج التنفيذ قد تتضمن محتوى رسائل (whatsapp/email) — VIEWER دوره
مراقبة الحالة لا قراءة المحتوى.

**الأخطاء:** `401 INVALID_TOKEN` · `403 PERMISSION_DENIED` · `404 RESOURCE_NOT_FOUND`.

---

## 2. `getEventsStream` — بث أحداث لحظي (SSE)

```
GET /events/stream
operationId: getEventsStream
```

**الهدف:** Realtime للـDashboard بدل الاستطلاع الدوري.

**البيانات المصدرية (بدون DDL جديد):** جدول `events` (id, event, source, project_id,
severity, payload, occurred_at, created_at) — cursor = آخر `event id` (ULID قابل
للترتيب).

**الصلاحيات:** OWNER + VIEWER (الـDashboard). **AGENT مستبعد** (أقل صلاحية — لا يحتاج
بثًا لحظيًا).

**المصادقة (قرار عقد صريح — الاستثناء الوحيد):** SSE عبر `EventSource` لا يستطيع
ضبط headers، لذلك هذا المسار فقط يقبل:
1. `Authorization: Bearer <token>` (عملاء fetch-based — المفضل)، أو
2. `?access_token=<token>` (لتمكين EventSource).
شرط موثَّق: الـtoken في URL يبقى في السجلات → يُنصح بـ **access tokens قصيرة
الصلاحية** لهذا المسار. لا يُقبل query-token في أي مسار آخر.

**Parameters (query):**
| الاسم | النوع | ملاحظة |
|---|---|---|
| `since` | string (event ULID) | اختياري — إن حُذفت = يبدأ من "الآن" (لا إعادة بث تاريخي كامل) |
| `types` | string (comma list) | اختياري — فلترة بنوع الحدث (قيم من enum `events.event` الموجود) |

**Response 200:** `Content-Type: text/event-stream`
```
: ping                       ← heartbeat كل 15 ثانية
id: ev_01…
event: task.created
data: {"id":"ev_01…","event":"task.created","severity":"INFO","payload":{…},"occurred_at":"…"}
```
- إعادة الاتصال: العميل يرسل `since=<آخر id استلمه>` — لا فقدان ولا تكرار (idULID
  monotonically ordered).
- التنفيذ المقترح (بلا تبعيات جديدة): استعلام زائد `id > cursor ORDER BY id` كل
  ثانيتين + flush — مناسب لمؤسسة واحدة في v1.1 (Redis pub/sub تحسين لاحق غير مطلوب).

**الأخطاء:** `401` · `403` فقط (لا 404 ولا 429 بتصميم — البث ليس موردًا ولا tier).
**Idempotency:** N/A (بث).
**ملاحظة أمان:** الـpayload يُمرَّر كما هو (سُجَّل عند حدوثه — نفس سياسة events
الموجودة)؛ لا تُضاف بيانات حساسة جديدة لأي حدث.

---

## 3. `listAutomations` — قائمة الأتمتة

```
GET /automations
operationId: listAutomations
```

**البيانات المصدرية (بدون DDL جديد):** جدول `automations` كاملاً.

**الصلاحيات:** OWNER + AGENT + VIEWER (قراءة).
**Parameters:** `enabled` (boolean, اختياري — فلترة).
**Rate limit:** بلا tier.

**Response 200:** `data` = مصفوفة من:
```json
{"id":"auto_01…","name":"…","description":"…","trigger":{…},"action":{…},
 "enabled":true,"last_run_at":"…","last_run_status":"SUCCESS",
 "next_run_at":"…","created_at":"…","updated_at":"…"}
```
(مصفوفة بلا pagination — نفس تخطيط قوائم v1؛ جدول الأتمتة محدود الحجم بطبيعته.)

**الأخطاء:** `401` · `403` · `400 VALIDATION_ERROR` (قيمة `enabled` غير صالحة).

---

## 4. `deleteAutomation` — حذف أتمتة

```
DELETE /automations/{automation_id}
operationId: deleteAutomation
header: Idempotency-Key  (مطلوبة — عملية كتابة قابلة لإعادة المحاولة)
```

**قرار سلوكي صريح:** **hard delete** (اسم العملية حذف، وجدول `automations` بلا عمود
حالة حذف — لا DDL). يُسجَّل في `audit_logs` (action `DELETE_AUTOMATION`) — السجل
غير القابل للتعديل هو الأرشيف.

**الصلاحيات:** **OWNER فقط** (لا أضع من PATCH الموجود — الحذف أخطر).
**Idempotency:** مطلوبة، TTL 24h كما في v1:
- نفس الـkey خلال 24h → نفس الاستجابة المحفوظة (204).
- حذف بدون key بعد الحذف → `404 RESOURCE_NOT_FOUND` (سلوك DELETE القياسي).
**Approval:** غير مطلوب (قرار موثَّق أعلاه).

**الاستجابات:** `204 No Content` · `400 VALIDATION_ERROR` (idempotency key ناقصة) ·
`401` · `403` · `404`.

---

## 5. `patchIncident` — تغيّر حالة حادث

```
PATCH /incidents/{incident_id}
operationId: patchIncident
header: Idempotency-Key  (مطلوبة — تغيّر حالة قابل لإعادة المحاولة)
```

**Body:**
```json
{"status": "RESOLVED", "resolution_note": "اختياري — يُسجَّل في audit فقط"}
```
`status` من enum الموجود في DDL: `OPEN | INVESTIGATING | MITIGATED | RESOLVED | CLOSED`.

**مصفوفة الانتقالات المسموح بها (قرار صريح — أي غير مسموح → `409 CONFLICT`):**
```
OPEN          → INVESTIGATING | RESOLVED
INVESTIGATING → MITIGATED | RESOLVED
MITIGATED     → RESOLVED | INVESTIGATING
RESOLVED      → CLOSED
CLOSED        → (حالة نهائية — لا انتقالات)
```
- التغيّر لنفس الحالة (no-op) → `409 CONFLICT` أيضًا (الحالة لا تحتاج تحديثًا).
- `resolved_at` يُضبط تلقائيًا عند الانتقال إلى `RESOLVED` (ويبقى كما هو عند
  `CLOSED`).

**مكان الـnote (بدون DDL جديد):** جدول `incidents` بلا عمود ملاحظة →
`resolution_note` **يُسجَّل حصريًا في `audit_logs.details`** (action `PATCH_INCIDENT`) —
قابل للاستعلام عبر `/audit` وغير قابل للتعديل.

**الصلاحيات:** OWNER + AGENT (الوِكيل هو من يُحلّل الحوادث التي رصدها —
`incidents.source` يدعم `agent` أصلًا). VIEWER لا يغيّر.
**Rate limit:** بلا tier. **Approval:** غير مطلوب (قرار موثَّق).

**Response 200:** `data` = كائن الحادث المحدّث كاملًا.
**الأخطاء:** `400` · `401` · `403` · `404` · `409 CONFLICT` (انتقال غير مسموح).

---

## 6. `getNotifications` — أرشيف الإشعارات

```
GET /notifications
operationId: getNotifications
```

**البيانات المصدرية (بدون DDL جديد):** جدول `notifications` كاملاً.

**الصلاحيات:** OWNER + VIEWER (أرشيف لوحة التحكم — AGENT لا يحتاجه، أقل صلاحية).
**Parameters (قوائم v1 بلا pagination، لكن الأرشيف ينمو بلا حد → استثناء موثَّق):**

| الاسم | النوع | ملاحظة |
|---|---|---|
| `severity` | enum | LOW\|MEDIUM\|HIGH\|CRITICAL |
| `status` | enum | QUEUED\|SENT\|FAILED\|DISMISSED |
| `project_id` | string | اختياري |
| `since` | datetime | اختياري |
| `limit` | int | default 50، max 200 — **الاستثناء الوحيد من "مصفوفة كاملة"** لأرشفة تنمو بلا حد |

**Response 200:** `data` = مصفوفة (الأحدث أولًا) من:
```json
{"id":"ntf_01…","severity":"HIGH","channel_requested":"WHATSAPP","channels":[…],
 "title":"…","message":"…","project_id":"prj_01…","status":"SENT",
 "data":{…},"created_at":"…","sent_at":"…"}
```

**الأخطاء:** `400` (حدود/قيم غير صالحة) · `401` · `403`.

---

## الملخص التنفيذي (عند الاعتماد)

| # | العملية | HTTP | أدوار | Idem. | Approval | DDL |
|---|---|---|---|---|---|---|
| 1 | getExecution | GET /agent/executions/{id} | OWNER·AGENT·VIEWER (result للـOWNER/AGENT فقط) | — | — | 0 |
| 2 | getEventsStream | GET /events/stream (SSE) | OWNER·VIEWER | — | — | 0 |
| 3 | listAutomations | GET /automations | OWNER·AGENT·VIEWER | — | — | 0 |
| 4 | deleteAutomation | DELETE /automations/{id} | OWNER | **مطلوبة** | — | 0 |
| 5 | patchIncident | PATCH /incidents/{id} | OWNER·AGENT | **مطلوبة** | — | 0 |
| 6 | getNotifications | GET /notifications | OWNER·VIEWER | — | — | 0 |

**ما لا يتغيّر في v1.1 (تأكيد صريح):**
- عقد v1: 40 عملية/مسار/schema — لا تغيّر واحد (additive فقط) → 46 عملية.
- أكواد الخطأ: نفس 15 الرمز.
- Rate tiers: نفس tier-ين (auth 5/دقيقة، agent 60/دقيقة).
- قائمة approval: نفس الأدوات الحساسة الحالية.
- DDL: `001_init.sql` كما هو — لا migration.
- الأمان: لا secrets في أي payload؛ تقييد VIEWER موثَّق أعلاه؛ استثناء
  query-token محصور في SSE فقط.

**قائمة الاختبار الدنيا عند التنفيذ (نفس منهجية v1 — تنفيذ حقيقي فقط):**
1. RBAC: كل دور × كل عملية جديدة (24 حالة) — OWNER/AGENT/VIEWER × المسموح/المرفوض.
2. getExecution: 404 غير موجود؛ VIEWER يستلم `result: null` وOWNER يستلم الكامل.
3. SSE: cursor من منتصف → استلام الأحداث اللاحقة فقط؛ heartbeat ≤15s؛ `types` filter؛
   `since` = آخر id → لا تكرار.
4. deleteAutomation: 204 ثم 404؛ replay بنفس الـkey → 204؛ بدون key → 400.
5. patchIncident: كل حافة مسموحة في المصفوفة 200؛ كل حافة ممنوعة 409؛ no-op 409؛
   `resolved_at` يُضبط عند RESOLVED؛ الـnote في audit فقط.
6. getNotifications: فلاتر severity/status/project_id/since؛ `limit` max 200 → 400.
7. audit: صفوف `DELETE_AUTOMATION`/`PATCH_INCIDENT` تظهر في `/audit` ومحاولة
   UPDATE/DELETE عليها مرفوضة بالـtrigger (كما في v1).
8. idempotency mismatch: نفس الـkey بجسم مختلف → 409 (نفس سلوك v1).

**خطوة القرار:** اعتماد هذا الملف (تعديل/تعليق على أي بند) → أُنزله إلى
`openapi/openapi.yaml` كـ `1.1.0` (مع تحديث `docs/api-contract-specification-v1.0.md`
إلى مرجع v1.1) + `app/generated/models.py` بالمولّد المثبت + التنفيذ + البطارية
الكاملة + CI. دون الاعتماد، عقد v1 يبقى كما هو 100%.
