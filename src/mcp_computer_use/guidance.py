"""Caller guidance (MCP instructions / tool descriptions) and question validation."""
from __future__ import annotations

import re
import unicodedata

INSTRUCTIONS = """\
mcp-computer-use: look_at_screen is a READ-ONLY screen reader. A local model reads one window on this computer and returns a short JSON answer, so you never pay for the raw window text.

When to use each tool:
- READ with look_at_screen: inspect window state, verify actions, or read text. For small/simple windows a direct read can be as cheap as the bridge; the bridge pays off on large, busy windows (chat apps, IDEs) and when `details` fields answer directly.
- Window IMAGE with include_image=true: request only when text is not enough (layout, charts, visual glitches); images cost thousands of tokens.
- ACT with click(app, target) or type_text(app, target, text): interact with visible UI labels (never @eN refs). Use steps=[...] for a known sequence of up to 10 labels. Read with look_at_screen first if unsure.
- SAFETY: Dangerous actions (Delete, Send, Pay, מחק, שלח, etc.) require human approval. You MUST ask the human user and get explicit approval before setting confirm_dangerous=true.

How to call look_at_screen:
- Write `question` in English (translate user input if needed). One short, focused question per call (max 300 chars).
- Pass `app` when known (e.g. "Claude", "ChatGPT", "מחשבון"); otherwise frontmost window is used.
- For multi-part checks call step by step: status first, then details.
- `details` is extracted by code for Claude Code, Codex desktop (ChatGPT), editors, and browsers (project, session, branch, next_step, failures, files, url); use it directly instead of asking again.
- Context across calls: pass `client` (e.g. "codex") and `session_id` (e.g. "task-1") to get `changed_since_last` and shorter answers. Omit them for independent calls.
- Memory: model stays loaded 3 min (single call) or 10 min (session or keep_warm=true). Call release_model() when your task is done.
- Health: call ping() to verify the bridge. If Ollama is down the server auto-starts it. On error follow error.suggestion (on OLLAMA_UNREACHABLE call health() first; on MODEL_TIMEOUT retry once).
"""

LOOK_DESCRIPTION = """\
Ask ONE short English question about what a window shows; get short JSON back
(status, summary, details, windows_seen, confidence, duration_seconds, memory_mb).
Read-only: never clicks/types. Pass `app` when known. Call step by step for multi-part checks.
`details` is filled by code: project, session, branch, next_step, failures, files.
`include_image`: optional JPEG (images cost thousands of tokens; request only when text is not enough).
`client` and `session_id` are OPTIONAL: without them each call is independent and no file is written; with them context is kept between calls (`changed_since_last`). A new session_id replaces the old one.
`keep_warm`: keep model loaded 10 min instead of 3 when asking soon.
Follow `error.suggestion` on error. Call release_model() when done."""

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
IMAGE_DESCRIPTION = (
    "Optional. Also return a downscaled JPEG of the target window (max 1024 px wide). "
    "Images cost thousands of tokens — request one only when text is not enough "
    "(layout, charts, visual glitches). Default false."
)

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
