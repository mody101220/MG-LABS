"""Tool Router — the registry between the agent brain and every executable action.

Contract rules implemented here:
  * tool names are the contract plan-step task names (SCREAMING_SNAKE) — the
    LLM may only emit these; anything else is rejected at plan-validation time;
  * every tool declares the (resource, action) the Permission Engine checks, so
    the LLM can never reach a service directly (architecture rule #30);
  * every tool declares a JSON Schema for its arguments — invalid arguments are
    rejected before execution (never at the provider's expense);
  * `external=True` tools route to an integration adapter — the ONLY path that
    touches a provider. `RESEARCH_FETCH` is declared (contract task) but no
    research provider exists in v1 core → honest TOOL_UNAVAILABLE at runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PLAN_STEPS = 10

_PRJ = {"type": "string", "pattern": "^prj_[a-z0-9]+$"}
_E164 = {"type": "string", "pattern": "^\\+[1-9]\\d{7,14}$"}
_EMAIL = {"type": "string", "pattern": "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"}
_DRAFT = {"type": "string", "pattern": "^draft_[a-z0-9]+$"}
_PRIORITY = {"type": "string", "enum": ["P0", "P1", "P2", "P3"]}
_MEMORY_TYPES = ["USER_PROFILE", "PROJECT_MEMORY", "CONTACT", "TASK", "DECISION",
                 "PREFERENCE", "DEADLINE", "CONVERSATION", "AUTOMATION"]
_TASK_STATUSES = ["TODO", "IN_PROGRESS", "IN_REVIEW", "DONE", "BLOCKED", "CANCELLED"]
_AUTOMATION_ACTIONS = ["PROJECT_MONITOR", "RUN_CHECK", "CREATE_TASK", "SEND_NOTIFICATION", "SEND_EMAIL_DRAFT"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_schema: dict
    resource: str
    action: str
    external: bool = False
    provider: str | None = None
    provider_action: str | None = None


def _obj(props: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TOOLS: dict[str, Tool] = {
    "GET_PROJECT_STATUS": Tool(
        "GET_PROJECT_STATUS",
        "Read the current status of one project (name, status, version).",
        _obj({"project_id": _PRJ}, ["project_id"]), "PROJECT", "READ"),
    "LIST_PROJECTS": Tool(
        "LIST_PROJECTS",
        "List the owner's projects with id, name, status and priority.",
        _obj({}), "PROJECT", "READ"),
    "CREATE_TASK": Tool(
        "CREATE_TASK",
        "Create a task in a project. The task starts as TODO.",
        _obj({"project_id": _PRJ, "title": {"type": "string", "minLength": 1, "maxLength": 200},
              "priority": _PRIORITY,
              "description": {"type": "string", "maxLength": 4000},
              "due_at": {"type": "string", "description": "RFC3339 date-time (UTC)"}},
             ["project_id", "title"]), "TASK", "CREATE"),
    "LIST_TASKS": Tool(
        "LIST_TASKS",
        "List tasks, optionally filtered by project or status.",
        _obj({"project_id": _PRJ, "status": {"type": "string", "enum": _TASK_STATUSES}}),
        "TASK", "READ"),
    "OPEN_INCIDENT": Tool(
        "OPEN_INCIDENT",
        "Open a critical incident record for a system or project.",
        _obj({"project_id": _PRJ, "issue": {"type": "string", "minLength": 1, "maxLength": 2000},
              "impact": {"type": "string", "maxLength": 2000}}, ["issue"]), "INCIDENT", "CREATE"),
    "SEND_WHATSAPP": Tool(
        "SEND_WHATSAPP",
        "Send a WhatsApp message to an E.164 number. Use only when the user explicitly asked to send.",
        _obj({"to": _E164, "message": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["to", "message"]),
        "WHATSAPP", "SEND", external=True, provider="whatsapp", provider_action="send"),
    "LIST_WA_INBOX": Tool(
        "LIST_WA_INBOX",
        "List recent inbound WhatsApp messages.",
        _obj({}), "WHATSAPP", "READ"),
    "DRAFT_EMAIL": Tool(
        "DRAFT_EMAIL",
        "Create an email draft (not sent). Returns the draft id.",
        _obj({"to": _EMAIL, "subject": {"type": "string", "maxLength": 300},
              "body": {"type": "string", "minLength": 1, "maxLength": 20000}},
             ["to", "subject", "body"]), "EMAIL", "DRAFT"),
    "SEND_EMAIL": Tool(
        "SEND_EMAIL",
        "Send an existing email draft (id from DRAFT_EMAIL). May require human approval.",
        _obj({"draft_id": _DRAFT}, ["draft_id"]), "EMAIL", "SEND", external=True, provider="email",
        provider_action="send"),
    "LIST_EMAILS": Tool(
        "LIST_EMAILS",
        "List recent email messages from a folder (default inbox).",
        _obj({"folder": {"type": "string", "enum": ["inbox", "sent"]},
              "limit": {"type": "integer", "minimum": 1, "maximum": 50}}), "EMAIL", "READ"),
    "CHECK_OPEN_ISSUES": Tool(
        "CHECK_OPEN_ISSUES",
        "Check open GitHub issues for the repository linked to a project.",
        _obj({"project_id": _PRJ}, ["project_id"]), "GITHUB", "READ",
        external=True, provider="github", provider_action="repo_status"),
    "CHECK_DEPLOYMENT": Tool(
        "CHECK_DEPLOYMENT",
        "Check recent deployment (actions) status for the repository linked to a project.",
        _obj({"project_id": _PRJ}, ["project_id"]), "GITHUB", "READ",
        external=True, provider="github", provider_action="repo_status"),
    "CREATE_GITHUB_ISSUE": Tool(
        "CREATE_GITHUB_ISSUE",
        "Create a GitHub issue in a registered repository (full_name owner/name).",
        _obj({"repository": {"type": "string", "pattern": "^[^/\\s]+/[^/\\s]+$"},
              "title": {"type": "string", "minLength": 1, "maxLength": 300},
              "body": {"type": "string", "maxLength": 20000},
              "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
              "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
             ["repository", "title"]), "GITHUB", "ISSUES",
        external=True, provider="github", provider_action="create_issue"),
    "SAVE_MEMORY": Tool(
        "SAVE_MEMORY",
        "Persist a long-term memory fact for the owner (upserted by type+key).",
        _obj({"type": {"type": "string", "enum": _MEMORY_TYPES},
              "key": {"type": "string", "pattern": "^[a-z0-9_.:-]{1,120}$"},
              "value": {"description": "any JSON value"},
              "project_id": _PRJ}, ["type", "key", "value"]), "MEMORY", "WRITE"),
    "SEARCH_MEMORY": Tool(
        "SEARCH_MEMORY",
        "Search long-term memory by type and/or key.",
        _obj({"type": {"type": "string", "enum": _MEMORY_TYPES}, "key": {"type": "string"}}),
        "MEMORY", "READ"),
    "CREATE_AUTOMATION": Tool(
        "CREATE_AUTOMATION",
        "Create a scheduled or event-driven automation.",
        _obj({"name": {"type": "string", "minLength": 1, "maxLength": 200},
              "description": {"type": "string", "maxLength": 4000},
              "trigger": {"type": "object",
                          "properties": {"type": {"type": "string", "enum": ["SCHEDULE", "EVENT"]},
                                         "cron": {"type": "string"},
                                         "event": {"type": "string"}},
                          "required": ["type"], "additionalProperties": False},
              "action": {"type": "object",
                         "properties": {"type": {"type": "string", "enum": _AUTOMATION_ACTIONS},
                                        "params": {"type": "object"}},
                         "required": ["type"], "additionalProperties": False}},
             ["name", "trigger", "action"]), "AUTOMATION", "CREATE"),
    "RESEARCH_FETCH": Tool(
        "RESEARCH_FETCH",
        "Research a topic (web research provider — not connected in v1 core; "
        "returns TOOL_UNAVAILABLE honestly).",
        _obj({"query": {"type": "string", "minLength": 3, "maxLength": 1000}}, ["query"]),
        "RESEARCH", "FETCH", external=True, provider="research"),
    "GET_SYSTEM_STATUS": Tool(
        "GET_SYSTEM_STATUS",
        "Get the overall system status (database, scheduler, integrations).",
        _obj({}), "SYSTEM", "READ"),
}


def lookup(name: str) -> Tool | None:
    return TOOLS.get(name)


def tools_for_llm() -> list[dict]:
    """OpenAI function-calling style tool descriptors for the system prompt."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.args_schema}}
            for t in TOOLS.values()]


# ── plan validation (before anything is persisted or executed) ──────────────

def _check_value(schema: dict, value, path: str) -> list[str]:
    problems: list[str] = []
    if value is None:
        return problems
    t = schema.get("type")
    if t == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']} chars")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']} chars")
        if "pattern" in schema and re.match(schema["pattern"], value) is None:
            problems.append(f"{path}: does not match required format")
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path}: expected integer"]
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above maximum {schema['maximum']}")
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: more than {schema['maxItems']} items")
    elif t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {sorted(schema['enum'])}")
    return problems


def validate_args(name: str, args: dict) -> list[str]:
    tool = TOOLS.get(name)
    if tool is None:
        return [f"unknown tool {name!r}"]
    if not isinstance(args, dict):
        return [f"{name}: args must be an object"]
    problems: list[str] = []
    for req in tool.args_schema.get("required", []):
        if req not in args or args[req] is None:
            problems.append(f"{name}: missing required arg {req!r}")
    for key, value in args.items():
        if key not in tool.args_schema.get("properties", {}):
            problems.append(f"{name}: unexpected arg {key!r}")
            continue
        problems.extend(_check_value(tool.args_schema["properties"][key], value, f"{name}.{key}"))
    # nested schemas (automation trigger/action)
    if name == "CREATE_AUTOMATION" and isinstance(args.get("trigger"), dict):
        trig = args["trigger"]
        ttype = trig.get("type")
        if ttype == "SCHEDULE" and not trig.get("cron"):
            problems.append("CREATE_AUTOMATION.trigger: SCHEDULE requires cron")
        if ttype == "EVENT" and not trig.get("event"):
            problems.append("CREATE_AUTOMATION.trigger: EVENT requires event")
    return problems


def validate_plan(steps: list[dict], max_steps: int = MAX_PLAN_STEPS) -> list[str]:
    """Full plan validation. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    if not isinstance(steps, list) or not steps:
        return ["plan: at least one step is required"]
    if len(steps) > max_steps:
        problems.append(f"plan: at most {max_steps} steps allowed (got {len(steps)})")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"step[{i}]: expected object")
            continue
        task = str(step.get("task") or "").strip().upper()
        if task not in TOOLS:
            problems.append(f"step[{i}]: unknown tool {step.get('task')!r}")
            continue
        args = step.get("args", {})
        problems.extend(validate_args(task, args))
    return problems
