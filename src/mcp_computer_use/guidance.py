"""Caller guidance (MCP instructions / tool descriptions) and question validation."""
from __future__ import annotations

import re
import unicodedata

INSTRUCTIONS = """\
mcp-computer-use: look_at_screen is a READ-ONLY screen reader. A local model reads one window on this computer and returns a short JSON answer, so you never pay for the raw window text.

How to call it:
- Always write `question` in English, even if the user speaks another language (translate it yourself).
- One short, focused, single-purpose question per call (max ~300 chars), e.g. "Is Claude working or finished?" or "What is Claude's last message about?".
- Pass `app` when you know it (e.g. "Claude", "ChatGPT"); otherwise the app named in the question or the frontmost app is used.
- For multi-part checks call several times, step by step: first the status, then the details. Do not pack several questions into one call.
- Optional context: pass `client` (your name, e.g. "codex", "claude") and a `session_id` you choose (e.g. "fix-ui"), and reuse them on every call of the same task. The server then keeps a small context file between calls: answers get shorter and more accurate, and `changed_since_last` tells you what changed since your previous call. Sending a NEW session_id replaces (deletes) your previous one for that client. Without them every call is independent: the full screen is re-read, "what changed" is not possible, and no file is written.
- `details` (filled by code, not the model) may hold project, session, branch, folder, next_step, failures, files, background_tasks; use it directly instead of asking again.
- To check the bridge works before a task, call ping() (the first ping can take 30-50 s while the model loads).
- If the local Ollama is down, the server starts it automatically (checked on calls without a session and on the first call of a session).
- Memory: the local model stays loaded 3 min after a single call and 10 min while a session is active (or with keep_warm=true), and is unloaded early if free RAM gets low. When your task is done, call release_model() to free the memory right away.
- Use click(app, target) or type_text(app, target, text) for app-scoped actions. Pass the visible label, not a ref; refs change between snapshots. Prefer one click per call when exploring; use click(app, steps=[...]) for a known sequence of up to 10 labels, such as calculator keys. Every step has a safety gate and a fresh snapshot; failure stops the batch. Read with look_at_screen first if unsure.
- Dangerous actions require human approval: You MUST ask the human user and get explicit approval before setting confirm_dangerous=true. Actions only log client/session_id; they do not create context sessions.
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
task; sending a new session_id replaces the old one for that client.
`keep_warm` (optional bool) keeps the model loaded 10 min instead of 3 when you will ask again soon.
Call release_model() when your task is done."""

PING_DESCRIPTION = """\
Heartbeat: checks that this bridge works end to end. Confirms the server is up, makes sure the local
Ollama is reachable (auto-starts it if needed) and asks the model for 'pong'. No screen reading.
Returns {ok, server, ollama, model_reply_ok, model, seconds, cold, error?}.
The first ping may take 30-50 s if the model is cold (not loaded); later pings take a few seconds."""

RELEASE_DESCRIPTION = """\
Unload the local model now and return freed memory_mb. Call it at the end of your task (after your last
look_at_screen) so the RAM is free again. Safe to call when nothing is loaded."""
KEEP_WARM_DESCRIPTION = ("Optional. true = keep the model loaded 10 min (session value) instead of 3 min, "
                         "when you will ask again soon without a session_id.")

HEALTH_DESCRIPTION = """\
Check the backend before/after errors: Ollama reachable, model pulled and loaded, memory, free RAM,
current keep_alive policy, agent-cu installed.
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


CONFIRM_DESCRIPTION = (
    "You MUST ask the human user and get explicit approval before setting confirm_dangerous=true. "
    "Default false; confirms a dangerous action only after that approval."
)
CLICK_DESCRIPTION = (
    "Click a visible label, or use steps for a known sequence (max 10), such as calculator keys. Prefer one click per call when exploring. "
    "Read with look_at_screen first if unsure. Pass the visible label, not a ref; refs change. "
    + CONFIRM_DESCRIPTION
)
TYPE_DESCRIPTION = (
    "Set one editable field's value in the named app, replacing its contents without submitting. "
    "Pass the visible label; refs change. Read with look_at_screen first if unsure. "
    + CONFIRM_DESCRIPTION
)
TARGET_DESCRIPTION = "Visible label (1 to 200 characters), not an @eN ref; refs change every snapshot. Add a role to clarify ambiguity."
TEXT_DESCRIPTION = "Text to set in the editable field, within max_type_chars. Never submitted or included in action results or logs."

STEPS_DESCRIPTION = (
    "Optional known sequence of 1 to 10 visible labels, e.g. calculator keys. Pass exactly one of target or steps. For a batch, omit target and supply steps. "
    "Every step is checked by the safety gate, with a fresh snapshot per step. Stops on the first failure. "
    "Pre-flight safety checks all step labels before any click. step_index is zero-based in pre-flight errors."
)
