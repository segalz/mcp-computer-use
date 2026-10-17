"""Code-based extraction: turn noisy accessibility text into a short status + last message.

No model is involved here; this is what keeps the prompt small and the answer accurate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Callable


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


# ---------------------------------------------------------------- Claude Desktop
CLAUDE_STOP_EXACT = {"New session", "Chat mode"}
CLAUDE_STOP_PREFIX = ("How is Claude doing",)
CLAUDE_DROP_EXACT = {"checked", "unchecked"}
CLAUDE_DROP_PREFIX = ("Ran", "Finished a background", "Used", "Captured", "Clicked")
CLAUDE_UI_NOISE = {"Environment", "unchecked", "checked", "Context", "Changes", "Chat mode"}


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
    tool_lines: list[str] = []
    if idx is not None:
        for ln in lines[idx + 2:]:
            s = ln.strip()
            if s in CLAUDE_STOP_EXACT or s.startswith(CLAUDE_STOP_PREFIX):
                break
            if not s or s in CLAUDE_DROP_EXACT:
                continue
            if s.startswith(CLAUDE_DROP_PREFIX) or s.startswith(", ran "):
                tool_lines.append(s)
                continue
            body.append(s)
    text, truncated = _cap("\n".join(body), max_chars)
    details = claude_text_details(lines)
    fails = find_failures(tool_lines + body)
    if fails:
        details["failures"] = fails
    nxt = find_next_step(body)
    if nxt:
        details["next_step"] = nxt
    if body and body[-1].rstrip().endswith("?"):
        details["asks_user"] = body[-1][-240:]
    return Extraction(app, "claude", status, text, sum(len(l) + 1 for l in lines), truncated, details)


def claude_text_details(lines: list[str]) -> dict:
    """Project / background tasks / context from the Claude Code side panel (text output)."""
    d: dict = {}
    stripped = [l.strip() for l in lines]
    for i, s in enumerate(stripped):
        if s == "Environment" and "project" not in d:
            for nxt in stripped[i + 1:i + 4]:
                if nxt and nxt not in CLAUDE_UI_NOISE:
                    d["project"] = nxt
                    break
        elif s.startswith("Background tasks ") and "background_tasks" not in d:
            d["background_tasks"] = s[len("Background tasks "):]
        elif re.fullmatch(r"\d{1,3}% used", s) and "context_used" not in d:
            d["context_used"] = s.split()[0]
    return d


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


# ---------------------------------------------------------------- snapshot (accessibility tree)
_NODE = re.compile(r'^(\s*)(?:\[@e\d+\]\s+)?([a-z]+)\s*(.*)$')
_LABEL = re.compile(r'^"(.*?)"((?:\s+val="[^"]*"|\s+id=\S+)*)\s*$')
_FILE = re.compile(r'^[^/\\:*?"<>|]{1,120}\.(exe|msi|zip|dmg|pkg|apk|appimage|deb|py|ts|tsx|js|json|md|txt|pdf|png|jpg|log|ps1|sh|toml|ya?ml|csv|html)$', re.I)
_WAIT_BUTTONS = {"allow", "allow once", "always allow", "deny", "approve", "reject", "accept", "decline"}


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


def claude_snapshot_details(snapshot: str) -> dict:
    """Session title, project, folder, branch, model, files, approval prompts from `agent-cu snapshot`."""
    d: dict = {}
    nodes = parse_snapshot(snapshot)
    last_you = max((i for i, n in enumerate(nodes) if n[2].startswith("You said:")), default=-1)
    files: list[str] = []
    for i, (ind, role, label) in enumerate(nodes):
        if not label:
            continue
        if role == "textarea" and label.endswith(" - Claude Code") and "session" not in d:
            d["session"] = label[: -len(" - Claude Code")].strip()
        elif role == "button" and label.endswith(", rename session") and "session" not in d:
            d["session"] = label[: -len(", rename session")].strip()
        elif role == "group" and label == "Environment":
            env = []
            for ind2, role2, label2 in nodes[i + 1:]:
                if ind2 <= ind:
                    break
                env.append((role2, label2))
            texts = [l for r, l in env if r == "statictext" and l and l != "Environment"]
            if texts and "project" not in d:
                d["project"] = texts[0]
            folder = next((l for r, l in env if r == "group" and re.match(r"^([A-Za-z]:\\|/|~)", l)), None)
            if folder:
                d["folder"] = folder
            skip = {"Environment", "Reorder Environment", "Remote Control", d.get("project")}
            branch = next((l for r, l in env if r == "button" and l and l not in skip), None)
            if branch:
                d["branch"] = branch
        elif role == "button" and label.startswith("Model: "):
            d["model"] = label[len("Model: "):]
        elif role == "group" and label.startswith("Background tasks "):
            d["background_tasks"] = label[len("Background tasks "):]
        elif role == "button" and label.lower() in _WAIT_BUTTONS:
            d["waiting_for_user"] = True
        if role == "button" and i > last_you and _FILE.match(label.strip()) and label not in files:
            files.append(label.strip())
    if files:
        d["files"] = files[-5:]
    return d


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


# app matcher -> snapshot detail parser (only these apps get the extra `agent-cu snapshot` call)
SNAPSHOT_DETAILS: list[tuple[Callable[[str], bool], Callable[[str], dict]]] = [
    (lambda name: "claude" in name, claude_snapshot_details),
]


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
