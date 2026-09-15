"""Agent Brain — the LLM-driven planner/summarizer of the rule #30 pipeline.

    LLM text -> IntentEngine(=LLM) -> Planner(=LLM, structured plan) ->
    Tool Router -> Permission Engine -> Action Executor -> ... ->
    Verification -> Audit  ->  LLM summary (best-effort) -> conversation

Responsibilities:
  * build the live context (projects, integrations, memory, conversation);
  * turn a natural-language command into a VALIDATED plan (the LLM never decides
    what is permitted — only what to attempt; validation + permissions do the rest);
  * one self-correction retry when the LLM's plan violates the registry;
  * honest failure: no LLM configured / provider down -> INTEGRATION_OFFLINE,
    never a silent rule-based fallback that would pretend to be the model;
  * summarize execution results into a user-facing reply, with a deterministic
    template fallback when the model is unavailable at summarization time.
"""
from __future__ import annotations

import json

from app.core import context
from app.core.config import Settings
from app.core.errors import AppError, validation_error
from app.generated import models
from app.core.security import Principal
from app.integrations.llm import OpenAICompatibleLLM

from . import prompts
from .intent import KNOWN_INTENTS
from .tools import validate_plan


class AgentBrain:
    def __init__(self, db, llm: OpenAICompatibleLLM, settings: Settings, adapters: dict) -> None:
        self._db = db
        self._llm = llm
        self._settings = settings
        self._adapters = adapters

    # ── context ────────────────────────────────────────────────────────────
    async def _context(self, principal: Principal, command: str, conversation_id: str | None) -> dict:
        projects, _ = await self._db.projects.list(1, 50)
        project_view = [{"id": p["id"], "name": p["name"], "status": p["status"], "priority": p["priority"]}
                        for p in projects]
        integrations: dict = {}
        for name in ("whatsapp", "email", "github"):
            adapter = self._adapters.get(name)
            row = await self._db.integrations.get(name)
            integrations[name] = {
                # adapter health is authoritative (env-derived credentials)
                "connected": adapter is not None and adapter.health().state == "CONNECTED",
                "permissions": (row or {}).get("permissions") or {},
            }
        memory_rows, _ = await self._db.memory.list(None, None, None, 1, 8)
        memory_view = [{"type": m["type"], "key": m["key"], "value": m["value"]} for m in memory_rows]
        history: list[dict] = []
        if conversation_id:
            history = await self._db.conversations.history(conversation_id, 10)
        return {
            "role": principal.role,
            "command": command,
            "projects": project_view,
            "integrations": integrations,
            "memory": memory_view,
            "history": history,
        }

    # ── planning ───────────────────────────────────────────────────────────
    async def plan(self, principal: Principal, command: str, conversation_id: str | None) -> dict:
        """Returns {"intent": str, "steps": [{"task", "args"}]}.

        Raises INTEGRATION_OFFLINE when no LLM is configured / provider down and
        VALIDATION_ERROR when the model's plan cannot be made valid.
        """
        if not self._llm.configured:
            raise AppError(models.ErrorCode.INTEGRATION_OFFLINE,
                           "The agent brain (LLM) is not configured",
                           {"reason": "GHAYATH_LLM_API_KEY is not set"})

        ctx = await self._context(principal, command, conversation_id)
        system = prompts.build_plan_system_prompt(ctx, self._settings.agent_max_plan_steps)
        user = prompts.build_plan_user_message(command)

        last_problems: list[str] = []
        for attempt in (1, 2):
            if attempt == 2:
                user = prompts.build_plan_retry_message(last_problems)
            result = await self._llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.1, max_tokens=1500, json_mode=True, purpose="agent.plan")
            parsed = self._parse(result.content)
            intent = str(parsed.get("intent") or "GENERAL").strip().upper()
            if intent not in KNOWN_INTENTS:
                intent = "GENERAL"
            raw_steps = parsed.get("steps")
            steps = []
            if isinstance(raw_steps, list):
                for s in raw_steps:
                    if not isinstance(s, dict):
                        continue
                    task = str(s.get("task") or "").strip().upper()
                    args = s.get("args")
                    steps.append({"task": task, "args": args if isinstance(args, dict) else {}})
            last_problems = validate_plan(steps, self._settings.agent_max_plan_steps)
            if not last_problems:
                context.log_event("agent.plan", {
                    "intent": intent, "steps": [s["task"] for s in steps],
                    "note": str(parsed.get("note") or "")[:200], "attempt": attempt,
                })
                return {"intent": intent, "steps": steps}
        context.log_event("agent.plan_rejected", {"problems": last_problems[:10]})
        raise validation_error({"problems": last_problems[:10]}, "Agent plan failed validation")

    @staticmethod
    def _parse(content: str | None) -> dict:
        if not content:
            return {}
        text = content.strip()
        # tolerate accidental markdown fences from non-compliant providers
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    obj = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    return {}
            else:
                return {}
        return obj if isinstance(obj, dict) else {}

    # ── summarization ──────────────────────────────────────────────────────
    async def summarize(self, principal: Principal, command: str,
                        plan: list[dict], results: list[dict]) -> str:
        """User-facing reply about the executed plan. Best-effort: when the model
        is unavailable the deterministic template (built from REAL step results)
        is used instead — still honest, never fabricated."""
        if self._llm.configured:
            try:
                msgs = prompts.build_summary_messages({"command": command}, plan, results)
                result = await self._llm.chat(msgs, temperature=0.3, max_tokens=400,
                                              json_mode=False, purpose="agent.summary")
                if result.content and result.content.strip():
                    return result.content.strip()
            except Exception as e:  # noqa: BLE001 — summarization must never kill an execution
                context.log_event("agent.summary_fallback", {"reason": type(e).__name__})
        return self._template_summary(plan, results)

    @staticmethod
    def _template_summary(plan: list[dict], results: list[dict]) -> str:
        done = [s for s in results if s["status"] == "COMPLETED"]
        failed = [s for s in results if s["status"] == "FAILED"]
        parts = []
        if done:
            parts.append(f"Completed {len(done)} step(s): " + ", ".join(s["task"] for s in done) + ".")
        for s in failed:
            reason = (s.get("result") or {}).get("error") or "unknown error"
            parts.append(f"Step {s['task']} failed: {reason}.")
        if not parts:
            parts.append("The plan could not be executed.")
        return " ".join(parts)
