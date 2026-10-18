"""Shared extraction result, text helpers, and accessibility-tree parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field


@dataclass
class Extraction:
    app: str
    extractor: str
    status: str          # working | finished | unknown
    last_message: str
    raw_chars: int       # size of the raw window text
    truncated: bool = False
    details: dict = field(default_factory=dict)  # project, session, next_step, failures, ...

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    # keep the END: the newest content is what callers care about
    return "…" + text[-(max_chars - 1):], True


# ---------------------------------------------------------------- shared detail finders
_FAIL = re.compile(r"\b(\d+\s+)?(failed|failing|failure|errors?|exception|traceback|crash(ed)?)\b|נכשל|שגיאה", re.I)
_NOT_FAIL = re.compile(r"\b(0|no|zero|without)\s+(failed|failures?|errors?)\b|\bno\s+\w+\s+failed\b|לא נכשל|בלי שגיאות", re.I)
_NEXT = re.compile(
    r"\b(next step|next,|next:|you (can|should|need to|may|might)|please|after (the )?(install|installing|restart|update|merge)"
    r"|re-?open|restart|install the|run the|try (again|it)|want me to|should i|shall i|let me know|ready (to|for))\b"
    r"|השלב הבא|אחרי ה|אפשר ל|צריך ל|תתקין|להתקין|תפתח|לפתוח מחדש|תריץ|להריץ|תרצה|רוצה ש|נא ל|כדאי ל", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+")


def find_failures(lines: list[str], limit: int = 3) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or not _FAIL.search(s) or _NOT_FAIL.search(s):
            continue
        s = s[:160]
        if any(s in o for o in out):
            continue
        out = [o for o in out if o not in s] + [s]  # keep the longer variant
    return out[-limit:]


def find_next_step(body: list[str], limit: int = 2) -> str:
    """Last sentences of the reply that suggest a next action (EN + HE)."""
    sents: list[str] = []
    for ln in body:
        sents.extend(x.strip(" ,") for x in _SENT.split(ln) if x.strip(" ,"))
    hits = [x for x in sents if _NEXT.search(x)]
    return " ".join(hits[-limit:])[:300]


_NODE = re.compile(r'^(\s*)(?:\[@e\d+\]\s+)?([a-z]+)\s*(.*)$')
_LABEL = re.compile(r'^"(.*?)"((?:\s+val="[^"]*"|\s+id=\S+)*)\s*$')


def parse_snapshot(text: str) -> list[tuple[int, str, str]]:
    nodes = []
    for ln in text.splitlines():
        m = _NODE.match(ln)
        if not m:
            continue
        indent, role, rest = len(m.group(1)), m.group(2), m.group(3)
        lm = _LABEL.match(rest)
        nodes.append((indent, role, lm.group(1) if lm else ""))
    return nodes


