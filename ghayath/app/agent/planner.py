"""Planner — turns (intent, command text) into an executable plan of steps.

Step `task` names are the planner task types referenced by the contract
(e.g. GET_PROJECT_STATUS, CHECK_OPEN_ISSUES, CHECK_DEPLOYMENT).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from app.generated import models

_PROJECT_REF_RE = re.compile(r"\bprj_[a-z0-9]+\b")


def extract_project_ref(text: str) -> str | None:
    m = _PROJECT_REF_RE.search(text or "")
    return m.group(0) if m else None


class Planner(ABC):
    @abstractmethod
    async def build_plan(self, intent: str, command_text: str) -> list[dict]: ...


class DefaultPlanner(Planner):
    """Deterministic step tables per intent. Every step maps to a ToolRouter tool."""

    _PLANS: dict[str, list[dict]] = {
        "PROJECT_MONITORING": [
            {"task": "GET_PROJECT_STATUS", "status": models.StepStatus.PENDING},
            {"task": "CHECK_OPEN_ISSUES", "status": models.StepStatus.PENDING},
            {"task": "CHECK_DEPLOYMENT", "status": models.StepStatus.PENDING},
        ],
        "INCIDENT_RESPONSE": [
            {"task": "OPEN_INCIDENT", "status": models.StepStatus.PENDING},
            {"task": "GET_PROJECT_STATUS", "status": models.StepStatus.PENDING},
        ],
        "TASK_MANAGEMENT": [
            {"task": "GET_PROJECT_STATUS", "status": models.StepStatus.PENDING},
        ],
        "EMAIL_COMPOSITION": [
            {"task": "LIST_EMAILS", "status": models.StepStatus.PENDING},
        ],
        "MESSAGE_REPLY": [
            {"task": "LIST_WA_INBOX", "status": models.StepStatus.PENDING},
        ],
        "RESEARCH": [
            {"task": "RESEARCH_FETCH", "status": models.StepStatus.PENDING},
        ],
        "GENERAL": [
            {"task": "GET_SYSTEM_STATUS", "status": models.StepStatus.PENDING},
        ],
    }

    async def build_plan(self, intent: str, command_text: str) -> list[dict]:
        steps = [dict(s, detail={}) for s in self._PLANS.get(intent, self._PLANS["GENERAL"])]
        project = extract_project_ref(command_text)
        if project:
            for s in steps:
                s["detail"]["project_id"] = project
        return steps
