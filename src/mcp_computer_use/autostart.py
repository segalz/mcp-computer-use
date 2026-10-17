"""Auto-start the LOCAL Ollama server when it is not reachable.

Only for a localhost OLLAMA_URL. Spawns `ollama serve` detached and hidden, with
OLLAMA_HOST=127.0.0.1:<port> set only for the child (the user's global OLLAMA_HOST is never touched).
A lock file in the sessions folder plus a port check prevent double starts.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .errors import CuError
from .sessions import sessions_dir

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCK_NAME = "ollama-autostart.lock"

# process-level flag reported by health()
state = {"autostarted": False, "last": None}


def target(cfg: Config) -> tuple[str, int] | None:
    """(host, port) if OLLAMA_URL points at this machine, else None."""
    u = urlparse(cfg.ollama_url)
    host = (u.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return None
    return ("127.0.0.1" if host == "localhost" else host), (u.port or 11434)


def find_ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if cand.is_file():
            return str(cand)
    return None


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _reachable(ollama) -> bool:
    try:
        ollama.models()
        return True
    except CuError:
        return False


def _spawn_impl(exe: str, host: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"{host}:{port}"  # child only
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "env": env, "close_fds": True}
    if sys.platform == "win32":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
        breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: survive the MCP client's job object
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        try:
            return subprocess.Popen([exe, "serve"], creationflags=flags | breakaway, startupinfo=si, **kw)
        except OSError:
            return subprocess.Popen([exe, "serve"], creationflags=flags, startupinfo=si, **kw)
    return subprocess.Popen([exe, "serve"], start_new_session=True, **kw)


_spawn = _spawn_impl  # indirection so tests can replace it


def _take_lock(path: Path, stale_after: float) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and time.time() - path.stat().st_mtime > stale_after:
            path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True  # cannot use a lock file: fall back to the port check only


def ensure_ollama(cfg: Config, ollama, sleep=None, clock=None) -> dict:
    """Make sure the local Ollama answers /api/tags. Returns what happened (never raises)."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    res = {"reachable": False, "started": False, "action": None, "seconds": 0.0}
    t0 = clock()
    if _reachable(ollama):
        res.update(reachable=True, action="already_running")
        return res
    tgt = target(cfg)
    if not cfg.autostart_ollama:
        res["action"] = "disabled"
        return res
    if not tgt:
        res["action"] = "not_local"
        return res
    host, port = tgt
    lock = sessions_dir(cfg) / LOCK_NAME
    spawn_needed = not port_open(host, port)
    owns_lock = False
    if spawn_needed:
        owns_lock = _take_lock(lock, stale_after=cfg.autostart_timeout + 5)
        if owns_lock:
            exe = find_ollama_exe()
            if not exe:
                _release(lock)
                res["action"] = "ollama_not_found"
                return res
            try:
                _spawn(exe, host, port)
            except OSError as e:
                _release(lock)
                res["action"] = f"spawn_failed: {e}"
                return res
            res["started"] = True
            res["action"] = "started"
        else:
            res["action"] = "waiting_for_other_start"  # another process is starting it
    else:
        res["action"] = "port_busy_waiting"  # something is listening; give it time
    try:
        deadline = t0 + cfg.autostart_timeout
        while clock() < deadline:
            if _reachable(ollama):
                res["reachable"] = True
                break
            sleep(0.5)
    finally:
        if owns_lock:
            _release(lock)
    res["seconds"] = round(clock() - t0, 2)
    if res["started"] and res["reachable"]:
        state["autostarted"] = True
    state["last"] = res
    return res


def _release(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass
