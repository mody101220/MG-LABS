"""Intent Engine — classifies a natural-language command into a contract intent.

v0 is a deterministic keyword-based classifier (Arabic + English). It is a REAL,
inspected implementation — swap in an LLM-backed classifier by implementing the same
interface; the rest of the pipeline is unchanged.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

KNOWN_INTENTS = (
    "PROJECT_MONITORING", "TASK_MANAGEMENT", "EMAIL_COMPOSITION",
    "MESSAGE_REPLY", "RESEARCH", "INCIDENT_RESPONSE", "GENERAL",
)

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("PROJECT_MONITORING", ("تابع", "تابعلي", "احكيلي عن", "حالة المشروع", "monitor", "status of", "check the project", "project status", "follow up")),
    ("INCIDENT_RESPONSE", ("طارئ", "حادثة", "ينهار", "down", "crash", "incident", "emergency", "فشل", "متوقف")),
    ("TASK_MANAGEMENT", ("مهمة", "ضيف", "أضف", "task", "todo", "أضف مهمة", "create task", "أرجع", "deadline")),
    ("EMAIL_COMPOSITION", ("ايميل", "بريد", "ارسلي بريد", "reply by email", "email", "compose")),
    ("MESSAGE_REPLY", ("رد", "ارسل", "رسالة", "واتساب", "reply", "send a message", "whatsapp", "text")),
    ("RESEARCH", ("ابحث", "بحث", "قارن", "research", "find out", "compare")),
]


class IntentEngine(ABC):
    @abstractmethod
    async def classify(self, text: str, context: dict | None = None) -> str: ...


class KeywordIntentEngine(IntentEngine):
    async def classify(self, text: str, context: dict | None = None) -> str:
        normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
        best, best_hits = "GENERAL", 0
        for intent, words in _KEYWORDS:
            hits = sum(1 for w in words if w.lower() in normalized)
            if hits > best_hits:
                best, best_hits = intent, hits
        return best
