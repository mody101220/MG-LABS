"""Deterministic OpenAI-compatible LLM test double (transport boundary).

Emulates a real model endpoint: same request/response wire format as
`/chat/completions` (json_mode, choices, usage). The 'brain' is keyword-driven
and documented per rule — it is a test double, NOT a production classifier.
Requests are recorded so tests can assert on what the model actually received
(system prompt content, data markers, tool schemas).
"""
from __future__ import annotations

import json
import re

import httpx

from app.agent.prompts import PLAN_MARKER
from app.integrations.llm import OpenAICompatibleLLM

USER_CMD_RE = re.compile(r"<user_command>\n?(.*?)\n?</user_command>", re.S)
PROJECT_LINE_RE = re.compile(r"^\s+(prj_[a-z0-9]+)\b", re.M)


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class ScriptedLLM:
    def __init__(self, responder=None) -> None:
        self.requests: list[dict] = []
        self.fail_next = 0  # number of next calls that fail with HTTP 500
        self.responder = responder or self.default_responder

    # ── scripted scenarios (tests may override `responder`) ────────────────
    @staticmethod
    def plan_unknown_tool(system: str, text: str) -> str:
        return _json({"intent": "GENERAL",
                      "steps": [{"task": "DROP_DATABASE", "args": {}}]})

    @staticmethod
    def plan_too_long(system: str, text: str) -> str:
        return _json({"intent": "GENERAL",
                      "steps": [{"task": "GET_SYSTEM_STATUS", "args": {}} for _ in range(15)]})

    # ── default deterministic brain ────────────────────────────────────────
    def default_responder(self, body: dict) -> str:
        messages = body["messages"]
        system = next(m["content"] for m in messages if m["role"] == "system")
        user = next(m["content"] for m in messages if m["role"] == "user")
        m = USER_CMD_RE.search(user)
        text = (m.group(1) if m else user).strip()
        low = text.lower()

        if PLAN_MARKER in system:
            return self._plan(system, text, low)
        return "تم تنفيذ الخطة. Plan executed as planned."

    def _plan(self, system: str, text: str, low: str) -> str:
        prj = re.search(r"prj_[a-z0-9]+", text)
        project_id = prj.group(0) if prj else self._first_project(system)

        if low in ("hello", "hi", "hello there") or "مرحبا" in low or "ahlan" in low:
            return _json({"intent": "GENERAL",
                          "steps": [{"task": "GET_SYSTEM_STATUS", "args": {}}]})
        if "project status" in low or "حالة المشروع" in text or "تابع" in text:
            steps = [{"task": "GET_PROJECT_STATUS", "args": {"project_id": project_id}}]
            if project_id:
                steps += [{"task": "CHECK_OPEN_ISSUES", "args": {"project_id": project_id}},
                          {"task": "CHECK_DEPLOYMENT", "args": {"project_id": project_id}}]
            return _json({"intent": "PROJECT_MONITORING", "steps": steps})
        if "send whatsapp" in low or "ارسل واتساب" in text or "رسالة واتساب" in text:
            return _json({"intent": "MESSAGE_REPLY",
                          "steps": [{"task": "SEND_WHATSAPP",
                                     "args": {"to": "+963990000001", "message": "تحية من GHAYATH"}}]})
        if "add a task" in low or "create task" in low or "أضف مهمة" in text or "أنشئ مهمة" in text:
            title = re.sub(r"^(أضف مهمة|أنشئ مهمة|add a task|create task|create a task)\s*", "",
                           text, flags=re.I).strip(" .!")
            return _json({"intent": "TASK_MANAGEMENT",
                          "steps": [{"task": "CREATE_TASK",
                                     "args": {"project_id": project_id, "title": title or "مهمة جديدة",
                                              "priority": "P1"}}]})
        if "remember that" in low or "احفظ أن" in text or "تذكر أن" in text:
            return _json({"intent": "GENERAL",
                          "steps": [{"task": "SAVE_MEMORY",
                                     "args": {"type": "DECISION", "key": "test.decision",
                                              "value": {"fact": text}}}]})
        if "research" in low or "ابحث" in text:
            return _json({"intent": "RESEARCH",
                          "steps": [{"task": "RESEARCH_FETCH", "args": {"query": text}}]})
        return _json({"intent": "GENERAL",
                      "steps": [{"task": "GET_SYSTEM_STATUS", "args": {}}]})

    @staticmethod
    def _first_project(system: str) -> str | None:
        m = PROJECT_LINE_RE.search(system)
        return m.group(1) if m else None


def make_llm_client(mock: ScriptedLLM) -> OpenAICompatibleLLM:
    """Production-shaped client pointed at the deterministic endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mock.requests.append(body)
        if mock.fail_next > 0:
            mock.fail_next -= 1
            return httpx.Response(500, json={"error": {"message": "mock LLM outage"}})
        content = mock.responder(body)
        return httpx.Response(200, json={
            "id": "chatcmpl-mock", "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
        })

    return OpenAICompatibleLLM(
        base_url="http://llm-mock.local/v1", api_key="mock-llm-key-not-real",
        model="mock-model-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
