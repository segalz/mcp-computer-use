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
- Optional context: pass `client` (your name, e.g. "codex", "claude") and a `session_id` you choose (e.g. "fix-ui"), and reuse them on every call of the same task. The server then keeps a small context file between calls: answers get shorter and more accurate, and `changed_since_last` tells you what changed since your previous call. Sending a NEW session_id replaces (deletes) your previous one for that client. Without them every call is independent: the full screen is re-read, "what changed" is not possible, and no file is written.
- `details` (filled by code, not the model) may hold project, session, branch, folder, next_step, failures, files, background_tasks; use it directly instead of asking again.
- It cannot click, type or change anything. Never ask it to.
- If the result has `error`, follow `error.suggestion`: on OLLAMA_UNREACHABLE call health() first; on MODEL_TIMEOUT retry once; on APP_NOT_FOUND retry with an `app` from `available_apps`; on QUESTION_TOO_LONG split the question. `extracted` holds the short raw text you can still use.
- A `hint` field is advice for your next call; the answer is still valid.
"""

LOOK_DESCRIPTION = """\
Ask ONE short English question about what a window on this computer shows; get short JSON back
(windows_seen, status, summary, confidence, details, model_used, duration_seconds, memory_mb).
`details` is filled by code: project, session, branch, next_step, failures, files, background_tasks when found.
Read-only: never clicks or types. Pass `app` when known. For multi-part checks call step by step
(status first, then details). On `error`, follow `error.suggestion`.
`client` and `session_id` are OPTIONAL. Without them every call is independent: the full screen is
re-read, no 'what changed' is possible and no file is written. With them the server keeps context
between calls (more accurate, shorter answers, plus `changed_since_last`). Reuse the same pair for one
task; sending a new session_id replaces the old one for that client."""

HEALTH_DESCRIPTION = """\
Check the backend before/after errors: Ollama reachable, model pulled and loaded, memory, agent-cu installed.
Call this first if look_at_screen returns OLLAMA_UNREACHABLE or MODEL_NOT_FOUND."""

QUESTION_DESCRIPTION = ("One short, focused question in English (max 300 chars), "
                        "e.g. 'Is Claude working or finished?'. Split bigger checks into several calls.")
APP_DESCRIPTION = "Optional app/window name, e.g. 'Claude' or 'ChatGPT'. Pass it when known."
CLIENT_DESCRIPTION = ("Optional. Your client name, e.g. 'codex' or 'claude' ([A-Za-z0-9_-], max 64). "
                      "Used with session_id to keep context between calls.")
SESSION_DESCRIPTION = ("Optional. A session name you choose, e.g. 'fix-ui' ([A-Za-z0-9_-], max 64). Reuse it on "
                       "every call of the same task to get context and 'changed_since_last'. A new value replaces "
                       "your previous session. Omit it and each call is independent (full re-read, no file).")

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
