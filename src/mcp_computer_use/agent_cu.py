"""Thin wrapper around agent-cu discovery, snapshots, and app-scoped actions."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import CuError, AGENT_CU_FAILED, AGENT_CU_NOT_INSTALLED, APP_NOT_FOUND, INVALID_TARGET

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


def _command_argv(exe: str) -> list[str] | None:
    """Resolve npm batch shims to Node directly, without executing batch syntax."""
    shim = Path(exe)
    if shim.suffix.lower() not in {".cmd", ".bat"}:
        return [exe]
    try:
        contents = shim.read_text(encoding="utf-8")
        match = re.search(r'"%dp0%[\\/]?(node_modules[\\/][^"\r\n]+\.(?:[cm]?js))"', contents, re.I)
        if not match:
            return None
        modules = (shim.parent / "node_modules").resolve()
        entry = (shim.parent / match[1].replace("\\", "/")).resolve()
        if not entry.is_relative_to(modules) or not entry.is_file():
            return None
        node = shim.parent / "node.exe"
        if not node.is_file():
            found = shutil.which("node.exe")
            if not found:
                return None
            node = Path(found)
        if node.suffix.lower() != ".exe" or not node.is_file():
            return None
        return [str(node), str(entry)]
    except (OSError, ValueError):
        return None


def _validate_app(app: str) -> None:
    if not isinstance(app, str) or not app or re.search(r'[&|<>^%"\r\n\x00]', app):
        raise CuError(INVALID_TARGET, "Pass an app name without shell metacharacters")


def run(args: list[str], cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    if "-a" in args:
        _validate_app(args[args.index("-a") + 1])
    exe = resolve_cmd(cmd)
    if not exe:
        raise CuError(AGENT_CU_NOT_INSTALLED, f"'{cmd}' not found on PATH")
    command = _command_argv(exe) or [exe]
    try:
        proc = subprocess.run([*command, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CuError(AGENT_CU_FAILED, f"agent-cu {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        raise CuError(AGENT_CU_FAILED, f"agent-cu could not start: {e}")
    if proc.returncode != 0:
        try:
            diagnostic = json.loads(proc.stderr or "")
        except json.JSONDecodeError:
            diagnostic = None
        kind = diagnostic.get("type") if isinstance(diagnostic, dict) else None
        if isinstance(kind, str):
            code = APP_NOT_FOUND if kind == "application_not_found" else AGENT_CU_FAILED
            raise CuError(code, f"agent-cu exited {proc.returncode}: {kind[:400]}")
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


def window_snapshot(app: str, cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    """Accessibility tree (interactive + compact, human format). Used only for extra details."""
    return run(["snapshot", "-a", app, "-i", "-c", "--human"], cmd, timeout)


@dataclass
class Element:
    ref: str
    role: str
    name: str
    val: str = ""
    id: str = ""
    context: str = ""


_NODE = re.compile(r'^(?P<indent>\s*)(?:\[(?P<ref>@e\d+)\]\s+)?(?P<role>[A-Za-z][\w-]*)\s*(?P<rest>.*)$')
_LABEL = re.compile(r'^"(?P<name>.*?)"(?P<attrs>(?:\s+val="[^"]*"|\s+id=\S+)*)\s*$')
_ATTR = re.compile(r'\s+(?:val="(?P<val>[^"]*)"|id=(?P<id>\S+))')
_REF = re.compile(r'@e\d+')


def snapshot_interactive(app: str, cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    return window_snapshot(app, cmd, timeout)


def parse_elements(snapshot: str) -> list[Element]:
    elements: list[Element] = []
    ancestors: list[tuple[int, str]] = []
    for line in snapshot.splitlines():
        node = _NODE.fullmatch(line)
        if not node:
            continue
        indent = len(node["indent"].expandtabs())
        while ancestors and ancestors[-1][0] >= indent:
            ancestors.pop()
        label = _LABEL.fullmatch(node["rest"])
        name = label["name"] if label else ""
        val = ident = ""
        if label:
            for attr in _ATTR.finditer(label["attrs"]):
                if attr["val"] is not None:
                    val = attr["val"]
                if attr["id"] is not None:
                    ident = attr["id"]
        if node["ref"]:
            elements.append(Element(node["ref"], node["role"], name, val, ident,
                                    ancestors[-1][1][:40] if ancestors else ""))
        if name:
            ancestors.append((indent, name))
    return elements


def _validate_ref(ref: str) -> None:
    if not isinstance(ref, str) or _REF.fullmatch(ref) is None:
        raise CuError(INVALID_TARGET, "Expected an agent-cu ref matching @e followed by digits")


def click(ref: str, app: str, cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    _validate_ref(ref)
    return run(["click", ref, "-a", app], cmd, timeout)


def type_text(text: str, app: str, ref: str, cmd: str = "agent-cu", timeout: float = 20.0) -> str:
    """Set AXValue only; never expose text-bearing diagnostics on failure."""
    _validate_ref(ref)
    _validate_app(app)
    kind = "os_error"
    code = AGENT_CU_FAILED
    try:
        exe = resolve_cmd(cmd)
        if not exe:
            code = AGENT_CU_NOT_INSTALLED
            kind = "not_installed"
        else:
            command = _command_argv(exe)
            if command is None:
                kind = "unsafe_shell_shim"
            else:
                return _type_direct(command, text, app, ref, timeout)
    except subprocess.TimeoutExpired:
        kind = "timeout"
    except OSError:
        kind = "os_error"
    except ValueError:
        kind = "value_error"
    # Raise outside exception handlers so no text-bearing exception is retained.
    raise CuError(code, f"agent-cu type {kind}: app={app[:100]!r} ref={ref[:40]}") from None


def _type_direct(command: list[str], text: str, app: str, ref: str, timeout: float) -> str:
    proc = subprocess.run([*command, "type", "-s", ref, "-a", app, "--", text],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode == 0:
        return proc.stdout or ""
    kind = f"exit_{proc.returncode}"
    try:
        diagnostic = json.loads(proc.stderr or "")
    except json.JSONDecodeError:
        diagnostic = None
    candidate = diagnostic.get("type") if isinstance(diagnostic, dict) else None
    if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,40}", candidate):
        kind = candidate
    code = APP_NOT_FOUND if kind == "application_not_found" else AGENT_CU_FAILED
    raise CuError(code, f"agent-cu type {kind}: app={app[:100]!r} ref={ref[:40]}") from None
