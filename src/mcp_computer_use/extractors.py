"""Code-based extraction: turn noisy accessibility text into a short status + last message.

No model is involved here; this is what keeps the prompt small and the answer accurate.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable


@dataclass
class Extraction:
    app: str
    extractor: str
    status: str          # working | finished | unknown
    last_message: str
    raw_chars: int       # size of the raw window text
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    # keep the END: the newest content is what callers care about
    return "…" + text[-(max_chars - 1):], True


# ---------------------------------------------------------------- Claude Desktop
CLAUDE_STOP_EXACT = {"New session", "Chat mode"}
CLAUDE_STOP_PREFIX = ("How is Claude doing",)
CLAUDE_DROP_EXACT = {"checked", "unchecked"}
CLAUDE_DROP_PREFIX = ("Ran", "Finished a background", "Used", "Captured", "Clicked")


def claude_status(lines: list[str]) -> str:
    if any("Claude is responding" in ln for ln in lines):
        return "working"
    if any("Claude finished the response" in ln for ln in lines):
        return "finished"
    return "unknown"


def extract_claude(lines: list[str], app: str, max_chars: int, **_) -> Extraction:
    lines = [ln.rstrip() for ln in lines]
    status = claude_status(lines)
    idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("You said:"):
            idx = i
            break
    body: list[str] = []
    if idx is not None:
        for ln in lines[idx + 2:]:
            s = ln.strip()
            if s in CLAUDE_STOP_EXACT or s.startswith(CLAUDE_STOP_PREFIX):
                break
            if not s or s in CLAUDE_DROP_EXACT or s.startswith(CLAUDE_DROP_PREFIX):
                continue
            body.append(s)
    text, truncated = _cap("\n".join(body), max_chars)
    return Extraction(app, "claude", status, text, sum(len(l) + 1 for l in lines), truncated)


# ---------------------------------------------------------------- generic fallback
def extract_generic(lines: list[str], app: str, max_chars: int, tail_chars: int = 2000, **_) -> Extraction:
    cleaned: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or s in {"checked", "unchecked"}:
            continue
        if cleaned and cleaned[-1] == s:  # collapse repeated lines
            continue
        cleaned.append(s)
    joined = "\n".join(cleaned)
    tail = joined[-tail_chars:] if len(joined) > tail_chars else joined
    text, truncated = _cap(tail, max_chars)
    truncated = truncated or len(joined) > tail_chars
    return Extraction(app, "generic", "unknown", text, len("\n".join(lines)), truncated)


# ---------------------------------------------------------------- registry
# (matcher on lower-cased app name, extractor). First match wins; generic is the fallback.
EXTRACTORS: list[tuple[Callable[[str], bool], Callable[..., Extraction]]] = [
    (lambda name: "claude" in name, extract_claude),
]


def extract(text: str, app: str, max_chars: int = 2000, tail_chars: int = 2000) -> Extraction:
    lines = text.splitlines()
    for match, fn in EXTRACTORS:
        if match(app.lower()):
            ex = fn(lines, app=app, max_chars=max_chars, tail_chars=tail_chars)
            # if the specialised extractor found nothing useful, fall back to generic tail
            if ex.last_message or ex.status != "unknown":
                return ex
            break
    return extract_generic(lines, app=app, max_chars=max_chars, tail_chars=tail_chars)
