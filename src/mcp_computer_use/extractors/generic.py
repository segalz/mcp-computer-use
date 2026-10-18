"""Generic tail extraction for windows without a specialized profile."""
from __future__ import annotations

from ._common import Extraction, _cap


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


