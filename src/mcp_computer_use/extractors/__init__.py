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
from .codex_desktop import extract_codex_desktop, codex_snapshot_details
from .registry import EXTRACTORS, SNAPSHOT_DETAILS, PROFILES, register_profile
from .registry import select_profile as _select_profile


def select_profile(app_name: str, window_title: str | None = None) -> str:
    profile = _select_profile(app_name)
    if profile == "generic" and isinstance(window_title, str) and "codex" in window_title.lower():
        return "codex_desktop"
    return profile


def wants_snapshot(app: str, window_title: str | None = None) -> bool:
    return (select_profile(app, window_title) == "codex_desktop"
            or any(m(app.lower()) for m, _ in SNAPSHOT_DETAILS))


def extract(text: str, app: str, max_chars: int = 2000, tail_chars: int = 2000,
            snapshot: str | None = None, window_title: str | None = None) -> Extraction:
    lines = text.splitlines()
    snapshot_nodes = parse_snapshot(snapshot) if isinstance(snapshot, str) and snapshot else None
    if window_title is None and snapshot_nodes is not None:
        window_title = next((label for _, role, label in snapshot_nodes if role == "window"), None)
    if select_profile(app, window_title) == "codex_desktop":
        return extract_codex_desktop(lines, app, max_chars, tail_chars, snapshot=snapshot, snapshot_nodes=snapshot_nodes)
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
