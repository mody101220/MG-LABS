# GHAYATH PERSONAL AI — API Contract Specification v1.0

| | |
|---|---|
| **Architecture Style** | Modular Agentic Architecture |
| **API Style** | REST/JSON |
| **Base URL** | `/api/v1` |
| **Authentication** | Bearer Token + RBAC |
| **Format** | `application/json` |
| **Time** | ISO-8601 UTC داخلياً، مع تحويل المنطقة الزمنية للعرض |
| **Version** | 1.0 |
| **Date** | 2026-09-15 |

> **المرافق الرسمية لهذا العقد:**
> - [OpenAPI 3.1 Specification](../openapi/openapi.yaml) — كل الـEndpoints أدناه قابلة للتحويل مباشرة إلى FastAPI/Node.js.
> - [Database Schema](../docs/database-schema.md) + [DDL](../sql/001_init.sql) — مخطط PostgreSQL الكامل.

---

## 1. API Architecture

```
CLIENT
  │
  ▼
API GATEWAY
  │
  ├── Authentication
  ├── Rate Limiting
  ├── Request Validation
  ├── Authorization
  │
  ▼
CORE API
  │
  ├── Agent API
  ├── Task API
  ├── Project API
  ├── Memory API
  ├── Automation API
  ├── Notification API
  └── Approval API
          │
          ▼
      TOOL ROUTER
          │
    ┌─────┼─────────────┐
    ▼     ▼             ▼
 WhatsApp Email       GitHub
    │     │             │
    └─────┼─────────────┘
          ▼
     EXECUTION ENGINE
          │
          ▼
     VERIFICATION
          │
          ▼
       AUDIT LOG
```

## 2. Standard API Response

كل Endpoint يجب أن يستخدم Response موحد.

**Success**

```json
{
  "success": true,
  "data": {},
  "request_id": "req_01J...",
  "timestamp": "2026-09-11T20:00:00Z"
}
```

**Error**

```json
{
  "success": false,
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "Required permission is missing",
    "details": {}
  },
  "request_id": "req_01J...",
  "timestamp": "2026-09-11T20:00:00Z"
}
```

## 3. Authentication API

### `POST /auth/login`

```json
{
  "email": "user@example.com",
  "password": "********"
}
```

Response:

```json
{
  "success": true,
  "data": {
    "access_token": "...",
    "refresh_token": "...",
    "expires_in": 3600,
    "user": {
      "id": "usr_001",
      "role": "OWNER"
    }
  }
}
```

### `POST /auth/refresh`

```json
{
  "refresh_token": "..."
}
```

### `POST /auth/logout`

Invalidates active session/token.

## 4. Agent API

### `POST /agent/command`

هذا هو الـEndpoint الرئيسي للأوامر الطبيعية.

**Request**

```json
{
  "command": "تابعلي مشروع SANAA",
  "source": "dashboard",
  "conversation_id": "conv_001",
  "autonomous": true
}
```

**Response**

```json
{
  "success": true,
  "data": {
    "command_id": "cmd_001",
    "intent": "PROJECT_MONITORING",
    "status": "PLANNED",
    "plan": [
      {
        "task": "GET_PROJECT_STATUS",
        "status": "PENDING"
      },
      {
        "task": "CHECK_OPEN_ISSUES",
        "status": "PENDING"
      },
      {
        "task": "CHECK_DEPLOYMENT",
        "status": "PENDING"
      }
    ]
  }
}
```

## 5. Agent Execution

### `POST /agent/execute`

يستخدم لتنفيذ Plan معتمد.

```json
{
  "command_id": "cmd_001",
  "approval_id": null
}
```

Response:

```json
{
  "success": true,
  "data": {
    "execution_id": "exec_001",
    "status": "RUNNING"
  }
}
```

**الحالات:**

| Status | الوصف |
|---|---|
| `PLANNED` | تم إنشاء الخطة، بانتظار التنفيذ |
| `WAITING_APPROVAL` | بانتظار موافقة المستخدم |
| `RUNNING` | قيد التنفيذ |
| `VERIFYING` | قيد التحقق من النتائج |
| `COMPLETED` | اكتمل وتم التحقق |
| `FAILED` | فشل التنفيذ |
| `BLOCKED` | محجوب (صلاحية/موافقة/إضافة غير متاحة) |
| `CANCELLED` | ألغي |

## 6. Projects API

### `GET /projects`

```json
{
  "success": true,
  "data": [
    {
      "id": "prj_sanaa",
      "name": "SANAA",
      "status": "ACTIVE",
      "priority": "P1",
      "version": "1.0"
    }
  ]
}
```

### `POST /projects`

```json
{
  "name": "SANAA",
  "description": "منصة طلب الخدمات والحرفيين",
  "priority": "P1"
}
```

### `GET /projects/{project_id}`

يعيد:

- Project
- Version
- Status
- Repository
- Deployment
- Tasks
- Bugs
- Security Issues
- Dependencies
- Integrations
- Last Activity
- Next Action

### `PATCH /projects/{project_id}`

```json
{
  "status": "ACTIVE",
  "version": "1.1",
  "priority": "P1",
  "next_action": "Production deployment"
}
```

## 7. Tasks API

### `POST /tasks`

```json
{
  "project_id": "prj_sanaa",
  "title": "فحص API authentication",
  "priority": "P1",
  "status": "TODO",
  "due_at": "2026-09-15T12:00:00Z"
}
```

### `GET /tasks`

Filters:

- `?project_id=`
- `?status=`
- `?priority=`
- `?assignee=`
- `?due_before=`

### `PATCH /tasks/{task_id}`

```json
{
  "status": "IN_PROGRESS"
}
```

### `POST /tasks/{task_id}/complete`

```json
{
  "verification": {
    "required": true
  }
}
```

> **لا يعتبر الـTask مكتمل فقط لأن الـAgent قال إنه انتهى.**
> الاكتمال يتطلب خطوة Verification مستقلة.

## 8. Memory API

### `POST /memory`

```json
{
  "type": "PROJECT_MEMORY",
  "key": "sanaa.current_version",
  "value": "1.0",
  "source": "verified"
}
```

**Memory Types**

| Type | الوصف |
|---|---|
| `USER_PROFILE` | معلومات المستخدم |
| `PROJECT_MEMORY` | ذاكرة خاصة بمشروع |
| `CONTACT` | جهة اتصال |
| `TASK` | ذاكرة مرتبطة بمهمة |
| `DECISION` | قرار سابق |
| `PREFERENCE` | تفضيل |
| `DEADLINE` | موعد نهائي |
| `CONVERSATION` | ذاكرة محادثة |
| `AUTOMATION` | ذاكرة أتمتة |

### `GET /memory`

```
GET /api/v1/memory?type=PROJECT_MEMORY&project_id=prj_sanaa
```

### `DELETE /memory/{memory_id}`

يتطلب صلاحية: `MEMORY.DELETE`

## 9. WhatsApp API

### `GET /integrations/whatsapp/status`

```json
{
  "connected": true,
  "provider": "whatsapp",
  "permissions": [
    "READ",
    "REPLY"
  ]
}
```

### `POST /whatsapp/messages/send`

```json
{
  "recipient": "+963XXXXXXXXX",
  "message": "مرحبا، محمد مشغول حالياً."
}
```

**قبل التنفيذ:**

```
AUTHORIZATION
      ↓
PERMISSION CHECK
      ↓
RECIPIENT VALIDATION
      ↓
CONTENT POLICY
      ↓
SEND
      ↓
VERIFY
      ↓
AUDIT
```

## 10. Incoming WhatsApp Webhook

### `POST /webhooks/whatsapp`

```json
{
  "event": "message.received",
  "message_id": "wamid.xxx",
  "sender": "+963XXXXXXXXX",
  "text": "مرحبا محمد، بدي احكي معك بخصوص المشروع",
  "timestamp": "2026-09-11T20:00:00Z"
}
```

**Agent يصنف:**

| Classification | الوصف |
|---|---|
| `PERSONAL` | شخصي |
| `BUSINESS` | عمل عام |
| `CLIENT` | عميل |
| `PROJECT` | مشروع محدد |
| `URGENT` | عاجل |
| `SPAM` | مزعج |
| `UNKNOWN` | غير معروف |

## 11. Email API

### `GET /email/messages`

```
GET /api/v1/email/messages?folder=inbox&priority=URGENT
```

### `GET /email/messages/{message_id}`

يعيد:

```json
{
  "id": "msg_001",
  "sender": "client@example.com",
  "subject": "Project Update",
  "priority": "HIGH",
  "classification": "CLIENT",
  "summary": "...",
  "required_action": "...",
  "deadline": null
}
```

## 12. Email Draft

### `POST /email/drafts`

```json
{
  "reply_to": "msg_001",
  "body": "شكراً لرسالتك..."
}
```

Response:

```json
{
  "draft_id": "draft_001",
  "status": "DRAFT"
}
```

## 13. Email Send

### `POST /email/send`

```json
{
  "draft_id": "draft_001"
}
```

إذا كانت الرسالة ذات تأثير عالي:

```json
{
  "success": false,
  "error": {
    "code": "APPROVAL_REQUIRED",
    "message": "Human approval is required before sending"
  }
}
```

## 14. GitHub API

### `GET /integrations/github/repositories`

### `GET /github/repos/{repo_id}/status`

Response:

```json
{
  "build": "PASSING",
  "tests": "PASSING",
  "deployment": "HEALTHY",
  "open_issues": 3,
  "security_alerts": 0
}
```

### `POST /github/issues`

```json
{
  "repository": "owner/repository",
  "title": "API timeout",
  "body": "Detected during monitoring",
  "priority": "P1"
}
```

## 15. Monitoring API

### `POST /monitoring/check`

```json
{
  "target_type": "PROJECT",
  "target_id": "prj_sanaa"
}
```

**يفحص:**

| Check | الوصف |
|---|---|
| `BUILD` | بناء المشروع |
| `DEPLOYMENT` | حالة النشر |
| `API` | استجابة الـAPI |
| `DATABASE` | قاعدة البيانات |
| `UPTIME` | توافر الخدمة |
| `ERRORS` | الأخطاء الأخيرة |
| `SECURITY` | تنبيهات أمنية |
| `CI/CD` | خطوط CI/CD |

## 16. Automation API

### `POST /automations`

مثلاً:

```json
{
  "name": "Monitor SANAA",
  "trigger": {
    "type": "SCHEDULE",
    "cron": "*/15 * * * *"
  },
  "action": {
    "type": "PROJECT_MONITOR",
    "target": "prj_sanaa"
  },
  "enabled": true
}
```

### `PATCH /automations/{automation_id}`

```json
{
  "enabled": false
}
```

## 17. Approval API

### `GET /approvals`

### `POST /approvals/{approval_id}/approve`

```json
{
  "approved": true
}
```

### `POST /approvals/{approval_id}/reject`

```json
{
  "reason": "Do not send this message"
}
```

## 18. Permission API

### `GET /permissions`

Response:

```json
{
  "WHATSAPP": {
    "READ": true,
    "REPLY": true,
    "SEND": false,
    "MEDIA": false
  },
  "EMAIL": {
    "READ": true,
    "DRAFT": true,
    "SEND": false,
    "DELETE": false
  },
  "GITHUB": {
    "READ": true,
    "ISSUES": true,
    "PULL_REQUEST": true,
    "COMMIT": false,
    "MERGE": false
  }
}
```

## 19. Permission Check

كل Action داخلي يجب أن يمر على:

### `POST /permissions/check`

```json
{
  "resource": "EMAIL",
  "action": "SEND",
  "target": "msg_001"
}
```

Response:

```json
{
  "allowed": false,
  "reason": "Human approval required"
}
```

## 20. Notification API

### `POST /notifications`

```json
{
  "severity": "CRITICAL",
  "channel": "PUSH",
  "title": "Production Down",
  "message": "SANAA production endpoint is unavailable"
}
```

**Routing:**

| Severity | القنوات |
|---|---|
| `CRITICAL` | PUSH + WHATSAPP |
| `HIGH` | PUSH |
| `MEDIUM` | DAILY BRIEF |
| `LOW` | DASHBOARD |

## 21. Audit API

### `GET /audit`

```
GET /api/v1/audit?project_id=prj_sanaa
```

**Record:**

```json
{
  "timestamp": "2026-09-11T20:15:00Z",
  "actor": "agent",
  "action": "PROJECT_MONITOR",
  "target": "prj_sanaa",
  "result": "SUCCESS",
  "execution_id": "exec_001"
}
```

> **Audit Log غير قابل للتعديل من الـAgent.**

## 22. Emergency Incident API

### `POST /incidents`

```json
{
  "severity": "CRITICAL",
  "system": "SANAA",
  "issue": "Database unavailable",
  "impact": "Production unavailable"
}
```

Response:

```json
{
  "incident_id": "inc_001",
  "status": "OPEN"
}
```

## 23. Health API

### `GET /health`

```json
{
  "status": "healthy",
  "services": {
    "database": "healthy",
    "redis": "healthy",
    "agent": "healthy",
    "scheduler": "healthy",
    "notifications": "healthy"
  }
}
```

## 24. System Status

### `GET /system/status`

```json
{
  "system": "OPERATIONAL",
  "agent": "READY",
  "automation": "ACTIVE",
  "security": "PROTECTED",
  "integrations": {
    "whatsapp": "CONNECTED",
    "email": "CONNECTED",
    "github": "CONNECTED"
  }
}
```

## 25. Daily Brief API

### `GET /brief/daily`

```json
{
  "date": "2026-09-11",
  "critical": [],
  "high": [],
  "tasks": [],
  "emails": [],
  "whatsapp": [],
  "projects": [],
  "deadlines": [],
  "actions_taken": [],
  "next_actions": []
}
```

## 26. Event Architecture

لا تعتمد على REST فقط.
استخدم Event Bus داخلي:

| Event | الوصف |
|---|---|
| `message.received` | وصل واتساب |
| `email.received` | وصل بريد |
| `task.created` | أُنشئت مهمة |
| `task.completed` | اكتملت مهمة (ومُتحقق منها) |
| `project.updated` | تغيّر مشروع |
| `deployment.failed` | فشل نشر |
| `security.alert` | تنبيه أمني |
| `approval.required` | مطلوبة موافقة |
| `approval.granted` | قُوبلت الموافقة |
| `approval.rejected` | رُفضت الموافقة |
| `automation.triggered` | انطلقت أتمتة |
| `incident.created` | حادثة طارئة |

**مثال:**

```json
{
  "event_id": "evt_001",
  "event": "deployment.failed",
  "source": "github",
  "project_id": "prj_sanaa",
  "severity": "HIGH",
  "timestamp": "2026-09-11T20:20:00Z"
}
```

## 27. Idempotency

هذه ضرورية جداً.

أي عملية ممكن تتكرر بسبب Retry يجب أن تدعم:

```
Idempotency-Key: 7c9a...
```

مثلاً إرسال رسالة:

```
Request #1 → SUCCESS
Request #2 بنفس Idempotency-Key
→ لا ترسل الرسالة مرة ثانية
```

هذا يمنع كارثة مثل إرسال نفس البريد أو الرسالة 20 مرة.

## 28. Rate Limiting

| مجموعة | الحد |
|---|---|
| Authentication | 5 requests/minute |
| Agent commands | 60 requests/minute |
| WhatsApp | Provider-dependent |
| Email | Provider-dependent |
| Monitoring | حسب Scheduler |

وعند التجاوز:

```
429 TOO MANY REQUESTS
```

## 29. API Error Codes

اعتمد مجموعة ثابتة:

| Code | HTTP | الوصف |
|---|---|---|
| `AUTHENTICATION_FAILED` | 401 | بيانات الدخول خاطئة |
| `INVALID_TOKEN` | 401 | Token منتهٍ أو غير صالح |
| `PERMISSION_DENIED` | 403 | الصلاحية مفقودة |
| `APPROVAL_REQUIRED` | 409 | مطلوبة موافقة بشرية قبل المتابعة |
| `VALIDATION_ERROR` | 400 | الطلب غير صالح |
| `RESOURCE_NOT_FOUND` | 404 | المورد غير موجود |
| `CONFLICT` | 409 | تعارض حالة |
| `RATE_LIMITED` | 429 | تجاوز حد الطلبات |
| `INTEGRATION_OFFLINE` | 503 | الخدمة الخارجية غير متصلة |
| `TOOL_UNAVAILABLE` | 503 | الأداة غير متاحة حالياً |
| `EXECUTION_FAILED` | 500 | فشل التنفيذ |
| `VERIFICATION_FAILED` | 500 | فشل التحقق من النتيجة |
| `TIMEOUT` | 504 | انتهت المهلة |
| `DEPENDENCY_FAILURE` | 500 | فشل اعتماد داخلي |
| `SECRET_ACCESS_DENIED` | 403 | محاولة وصول لسر/مفتاح |

## 30. أهم قاعدة معمارية

**لا تسمح للـLLM باستدعاء الخدمات الحساسة مباشرة.**

خطأ:

```
LLM
  ↓
Gmail API
```

الصحيح:

```
LLM
  ↓
Planner
  ↓
Tool Router
  ↓
Permission Engine
  ↓
Action Executor
  ↓
External API
  ↓
Verification
  ↓
Audit Log
```

وهذا بالضبط هو الفرق بين Chatbot عنده Tools وبين Autonomous Operations Agent مضبوط هندسياً.

---

## الملحق A: الـMVP الذي يُنصح ببنائه أولاً

لا تبدأ بـ20 Integration. هذا سيضخم المشروع ويخليه هش.
ابدأ بهذا:

```
CORE AGENT
     │
     ├── PostgreSQL
     ├── Redis
     ├── Memory
     ├── Tasks
     ├── Projects
     ├── Permissions
     ├── Approvals
     ├── Audit Log
     └── Scheduler
             │
             ├── Gmail
             ├── WhatsApp
             └── GitHub
```

وبعد ما ينجح هذا الـCore، أضف بقية الخدمات.
