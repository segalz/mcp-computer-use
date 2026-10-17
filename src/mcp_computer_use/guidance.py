"""Caller guidance (MCP instructions / tool descriptions) and question validation."""
from __future__ import annotations

import re
import unicodedata

INSTRUCTIONS = """\
mcp-computer-use: a READ-ONLY screen reader. A local model reads one window on this computer and returns a short JSON answer, so you never pay for the raw window text.

How to call it:
- Always write `question` in English, even if the user speaks another language (translate it yourself).
- One short, focused, single-purpose question per call (max ~300 chars), e.g. "Is Claude working or finished?" or "What is Claude's last message about?".
- Pass `app` when you know it (e.g. "Claude", "ChatGPT"); otherwise the app named in the question or the frontmost app is used.
- For multi-part checks call several times, step by step: first the status, then the details. Do not pack several questions into one call.
- It cannot click, type or change anything. Never ask it to.
- If the result has `error`, follow `error.suggestion`: on OLLAMA_UNREACHABLE call health() first; on MODEL_TIMEOUT retry once; on APP_NOT_FOUND retry with an `app` from `available_apps`; on QUESTION_TOO_LONG split the question. `extracted` holds the short raw text you can still use.
- A `hint` field is advice for your next call; the answer is still valid.
"""

LOOK_DESCRIPTION = """\
Ask ONE short English question about what a window on this computer shows; get short JSON back
(windows_seen, status, summary, confidence, model_used, duration_seconds, memory_mb).
Read-only: never clicks or types. Pass `app` when known. For multi-part checks call step by step
(status first, then details). On `error`, follow `error.suggestion`."""

HEALTH_DESCRIPTION = """\
Check the backend before/after errors: Ollama reachable, model pulled and loaded, memory, agent-cu installed.
Call this first if look_at_screen returns OLLAMA_UNREACHABLE or MODEL_NOT_FOUND."""

QUESTION_DESCRIPTION = ("One short, focused question in English (max 300 chars), "
                        "e.g. 'Is Claude working or finished?'. Split bigger checks into several calls.")
APP_DESCRIPTION = "Optional app/window name, e.g. 'Claude' or 'ChatGPT'. Pass it when known."

HINT_MULTIPART = ("This looks like several questions in one; for best results call look_at_screen "
                  "step by step, one focused question per call (status first, then details).")
HINT_ENGLISH = "Ask in English for best results."

_NUMBERED = re.compile(r"(?:^|\s)(?:\d+[.)]|[-*•])\s+\S", re.M)


def is_multipart(q: str) -> bool:
    return q.count("?") >= 3 or len(_NUMBERED.findall(q)) >= 2


def mostly_non_latin(q: str) -> bool:
    letters = [c for c in q if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters) < 0.5


def question_hints(q: str) -> list[str]:
    hints = []
    if is_multipart(q):
        hints.append(HINT_MULTIPART)
    if mostly_non_latin(q):
        hints.append(HINT_ENGLISH)
    return hints
