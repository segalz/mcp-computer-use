"""Thin, read-only wrapper around the `agent-cu` CLI (only `apps` and `text` are used)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

from .errors import CuError, AGENT_CU_FAILED, AGENT_CU_NOT_INSTALLED, APP_NOT_FOUND

# question keywords (incl. Hebrew) -> substring of the app name
APP_ALIASES = {
    "claude": ("claude", "קלוד"),
    "chatgpt": ("codex", "chatgpt", "קודקס", "צ'אט"),
    "code": ("vs code", "vscode", "visual studio code"),
    "cursor": ("cursor", "קרסר"),
    "chrome": ("chrome", "כרום", "browser", "דפדפן"),
    "powershell": ("powershell", "terminal", "טרמינל"),
    "notepad": ("notepad", "פנקס"),
}


@dataclass
class AppInfo:
    name: str
    frontmost: bool = False
    title: str = ""


def resolve_cmd(cmd: str) -> str | None:
    # On Windows npm installs `agent-cu.cmd`; shutil.which honours PATHEXT.
    return shutil.which(cmd)


def run(args: list[str], cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    exe = resolve_cmd(cmd)
    if not exe:
        raise CuError(AGENT_CU_NOT_INSTALLED, f"'{cmd}' not found on PATH")
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CuError(AGENT_CU_FAILED, f"agent-cu {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        raise CuError(AGENT_CU_FAILED, f"agent-cu could not start: {e}")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        if re.search(r"not found|no (such )?app|no window", err, re.I):
            raise CuError(APP_NOT_FOUND, f"agent-cu: {err}")
        raise CuError(AGENT_CU_FAILED, f"agent-cu {' '.join(args)} exited {proc.returncode}: {err}")
    return proc.stdout or ""


def parse_apps(stdout: str) -> list[AppInfo]:
    """Accepts a JSON list of strings or objects, or {"apps": [...]}. Tolerant of field names."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [AppInfo(name=l.strip()) for l in stdout.splitlines() if l.strip()]
    if isinstance(data, dict):
        data = data.get("apps") or data.get("applications") or data.get("items") or []
    apps: list[AppInfo] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            apps.append(AppInfo(name=item))
        elif isinstance(item, dict):
            name = item.get("name") or item.get("app") or item.get("processName") or item.get("title") or ""
            if not name:
                continue
            front = bool(item.get("frontmost") or item.get("focused") or item.get("active")
                         or item.get("isFrontmost") or item.get("foreground"))
            apps.append(AppInfo(name=str(name), frontmost=front, title=str(item.get("title") or "")))
    return apps


def list_apps(cmd: str = "agent-cu", timeout: float = 20.0) -> list[AppInfo]:
    return parse_apps(run(["apps"], cmd, timeout))


def choose_app(apps: list[AppInfo], question: str) -> AppInfo | None:
    if not apps:
        return None
    q = question.lower()
    # 1) app name literally mentioned in the question
    for a in apps:
        if a.name and a.name.lower() in q:
            return a
    # 2) alias keyword in the question
    for target, words in APP_ALIASES.items():
        if any(w in q for w in words):
            for a in apps:
                if target in a.name.lower() or target in a.title.lower():
                    return a
    # 3) frontmost window, else first
    for a in apps:
        if a.frontmost:
            return a
    return apps[0]


def find_app(apps: list[AppInfo], requested: str) -> AppInfo | None:
    r = requested.lower()
    for a in apps:
        if a.name.lower() == r:
            return a
    for a in apps:
        if r in a.name.lower() or r in a.title.lower():
            return a
    return None


def window_text(app: str, cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    return run(["text", "-a", app], cmd, timeout)
