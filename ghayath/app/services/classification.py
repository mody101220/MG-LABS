"""Message classification (contract §10): PERSONAL | BUSINESS | CLIENT | PROJECT |
URGENT | SPAM | UNKNOWN. Deterministic v0 rules; swappable for a model-based
classifier without touching the pipeline."""
from __future__ import annotations

import re

_URGENT = re.compile(r"(عاجل|طارئ|urgent|asap|فورا|now|emergency|حالة طارئة)", re.I)
_SPAM = re.compile(r"(won a prize|lottery|click here to claim|مبارك لقد فزت|عرض خاص محدود|spam)", re.I)
_CLIENT = re.compile(r"(project update|invoice|فاتورة|عميل|client|contract|عقد|delivery|تسليم|deadline of)", re.I)
_PROJECT = re.compile(r"(prj_[a-z0-9]+|sanad|sanaa|deployment|نشر|build|api|مستودع|repo)", re.I)
_BUSINESS = re.compile(r"(meeting|اجتماع|شركاء|partner|vendor|مورد|budget|ميزانية|meeting)", re.I)
_PERSONAL = re.compile(r"(مرحبا|hi |hello|اخبارك|how are you|عشاء|غدا |coffee|قهوة|family|عائلة)", re.I)


def classify_message(text: str) -> str:
    t = text or ""
    if _URGENT.search(t):
        return "URGENT"
    if _SPAM.search(t):
        return "SPAM"
    if _CLIENT.search(t):
        return "CLIENT"
    if _PROJECT.search(t):
        return "PROJECT"
    if _BUSINESS.search(t):
        return "BUSINESS"
    if _PERSONAL.search(t):
        return "PERSONAL"
    return "UNKNOWN"
