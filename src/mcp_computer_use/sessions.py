"""Optional per-client session files (no in-memory state).

One JSON file per (client, session_id) in the sessions folder: `{client}__{session_id}.json`.
A client that sends a new session_id gets its previous file(s) deleted, so each client has
exactly one active file. Files not updated for `session_ttl_hours` are removed on every call.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

MAX_ID = 64
MAX_QUESTIONS = 5
DEFAULT_CLIENT = "default"
_BAD = re.compile(r"[^A-Za-z0-9_-]")
TRACKED = ("status", "project", "session", "branch", "next_step", "failures",
           "background_tasks", "waiting_for_user", "files")


def sanitize(value: str | None) -> str | None:
    if value is None:
        return None
    v = _BAD.sub("", str(value))[:MAX_ID]
    return v or None


def sessions_dir(cfg: Config) -> Path:
    if cfg.sessions_dir:
        return Path(cfg.sessions_dir)
    # editable/source install: <project>/.sessions next to pyproject.toml
    here = Path(__file__).resolve()
    for parent in here.parents[:4]:
        if (parent / "pyproject.toml").is_file():
            return parent / ".sessions"
    return Path.home() / ".mcp-computer-use" / "sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cleanup(folder: Path, ttl_hours: float) -> int:
    if not folder.is_dir() or ttl_hours <= 0:
        return 0
    limit = time.time() - ttl_hours * 3600
    removed = 0
    for f in folder.glob("*__*.json"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def screen_hash(last_message: str, details: dict, status: str) -> str:
    blob = json.dumps({"m": last_message, "d": details, "s": status}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


class Session:
    def __init__(self, folder: Path, client: str, session_id: str):
        self.folder, self.client, self.session_id = folder, client, session_id
        self.path = folder / f"{client}__{session_id}.json"
        self.replaced: list[str] = []
        self.data: dict = {}
        self.is_new = True

    @classmethod
    def open(cls, cfg: Config, client: str | None, session_id: str) -> "Session":
        folder = sessions_dir(cfg)
        folder.mkdir(parents=True, exist_ok=True)
        cleanup(folder, cfg.session_ttl_hours)
        s = cls(folder, client or DEFAULT_CLIENT, session_id)
        for old in folder.glob(f"{s.client}__*.json"):
            if old.name != s.path.name:  # new session_id replaces the old one for this client
                try:
                    old.unlink()
                    s.replaced.append(old.stem.split("__", 1)[1])
                except OSError:
                    pass
        if s.path.is_file():
            try:
                s.data = json.loads(s.path.read_text(encoding="utf-8"))
                s.is_new = False
            except (OSError, ValueError):
                s.data = {}
        return s

    def snapshot_state(self, app: str, status: str, last_message: str, details: dict) -> dict:
        st = {"app": app, "status": status, "last_message": last_message[:800]}
        for k in TRACKED:
            if k != "status" and details.get(k) not in (None, "", [], False):
                st[k] = details[k]
        st["screen_hash"] = screen_hash(last_message, details, status)
        return st

    def changes(self, new: dict) -> list[str]:
        """Short list of what changed since the previous call (empty = nothing)."""
        old = self.data
        if self.is_new or not old:
            return []
        out = []
        if old.get("app") != new.get("app"):
            out.append(f"app: {old.get('app')} -> {new.get('app')}")
        for k in TRACKED:
            a, b = old.get(k), new.get(k)
            if a == b:
                continue
            if k in ("next_step", "failures", "files"):
                out.append(f"{k} {'cleared' if not b else 'updated'}: {b if b else ''}".rstrip(": "))
            else:
                out.append(f"{k}: {a if a not in (None, '') else '(none)'} -> {b if b not in (None, '') else '(none)'}")
        if old.get("last_message") != new.get("last_message"):
            out.append("new text in the last message")
        return out

    def cached_answer(self, question: str, new_hash: str) -> dict | None:
        """Same question, unchanged screen -> reuse the previous answer (no model call)."""
        if self.data.get("screen_hash") != new_hash:
            return None
        for q in reversed(self.data.get("questions", [])):
            if q.get("q", "").strip().lower() == question.strip().lower() and q.get("full"):
                return q["full"]
        return None

    def save(self, state: dict, question: str, answer: dict) -> None:
        qs = list(self.data.get("questions", []))
        full = {k: answer[k] for k in ("status", "summary", "confidence") if k in answer}
        qs.append({"q": question[:300], "a": str(answer.get("summary", ""))[:300], "at": _now(), "full": full})
        data = {"client": self.client, "session_id": self.session_id,
                "created_at": self.data.get("created_at") or _now(), "updated_at": _now(),
                **state, "questions": qs[-MAX_QUESTIONS:]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self.data, self.is_new = data, False

    def recent_questions(self, n: int = 3) -> list[dict]:
        return [{"q": q.get("q"), "a": q.get("a")} for q in self.data.get("questions", [])[-n:]]
