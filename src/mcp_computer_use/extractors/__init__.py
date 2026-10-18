"""Code-based extraction: turn noisy accessibility text into a short status + last message.

No model is involved here; this is what keeps the prompt small and the answer accurate.
"""
from __future__ import annotations

from ._common import (
    Extraction, _cap, _FAIL, _NOT_FAIL, _NEXT, _SENT, _NODE, _LABEL,
    find_failures, find_next_step, parse_snapshot,
)
from .claude_code import (
    CLAUDE_STOP_EXACT, CLAUDE_STOP_PREFIX, CLAUDE_DROP_EXACT, CLAUDE_DROP_PREFIX,
    CLAUDE_UI_NOISE, _FILE, _WAIT_BUTTONS,
    claude_status, extract_claude, claude_text_details, claude_snapshot_details,
)
from .generic import extract_generic
from .registry import EXTRACTORS, SNAPSHOT_DETAILS, PROFILES, register_profile, select_profile


def wants_snapshot(app: str) -> bool:
    return any(m(app.lower()) for m, _ in SNAPSHOT_DETAILS)


def extract(text: str, app: str, max_chars: int = 2000, tail_chars: int = 2000,
            snapshot: str | None = None) -> Extraction:
    lines = text.splitlines()
    ex = None
    for match, fn in EXTRACTORS:
        if match(app.lower()):
            ex = fn(lines, app=app, max_chars=max_chars, tail_chars=tail_chars)
            # if the specialised extractor found nothing useful, fall back to generic tail
            if not (ex.last_message or ex.status != "unknown"):
                ex = None
            break
    if ex is None:
        ex = extract_generic(lines, app=app, max_chars=max_chars, tail_chars=tail_chars)
        fails = find_failures(ex.last_message.splitlines()[-30:])
        if fails:
            ex.details["failures"] = fails
    if snapshot:
        for match, fn in SNAPSHOT_DETAILS:
            if match(app.lower()):
                try:
                    snap = fn(snapshot)
                except Exception:  # never let detail parsing break the answer
                    snap = {}
                for k, v in snap.items():
                    ex.details.setdefault(k, v)
                break
    return ex
