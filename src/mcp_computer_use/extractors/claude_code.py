"""Claude Code profile, retaining the existing Claude extraction behavior."""
from __future__ import annotations

import re

from ._common import Extraction, _cap, find_failures, find_next_step, parse_snapshot


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


_FILE = re.compile(r'^[^/\\:*?"<>|]{1,120}\.(exe|msi|zip|dmg|pkg|apk|appimage|deb|py|ts|tsx|js|json|md|txt|pdf|png|jpg|log|ps1|sh|toml|ya?ml|csv|html)$', re.I)
_WAIT_BUTTONS = {"allow", "allow once", "always allow", "deny", "approve", "reject", "accept", "decline"}


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


