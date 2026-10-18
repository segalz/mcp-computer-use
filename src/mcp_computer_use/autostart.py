"""Auto-start the LOCAL Ollama server when it is not reachable.

Only for a localhost OLLAMA_URL. Spawns `ollama serve` detached and hidden, with
OLLAMA_HOST=127.0.0.1:<port> set only for the child (the user's global OLLAMA_HOST is never touched).
A lock file in the sessions folder plus a port check prevent double starts.

Windows: the server must outlive the MCP host. Hosts such as `codex exec` run the bridge inside a
job object that kills every member when the host exits. The child is first started with
CREATE_BREAKAWAY_FROM_JOB; if that is refused, or the child is still in a job, it is started through
WMI Win32_Process.Create instead, whose processes are parented by WmiPrvSE and belong to no job of
the host. Server output goes to <sessions_dir>/ollama-autostart.log.
"""
from __future__ import annotations

import os
import re
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
LOG_NAME = "ollama-autostart.log"
WMI_TIMEOUT = 15.0  # seconds for the PowerShell/WMI launcher (budgeted apart from the readiness poll)

# process-level flag reported by health(); "method" = how the last spawn was done
state = {"autostarted": False, "last": None, "method": None}


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


_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Win32_Process.Create with a hidden window; inputs come from the environment, never the script text
_WMI_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
    "@{CommandLine=$env:MCP_CU_WMI_CMDLINE; CurrentDirectory=$env:MCP_CU_WMI_CWD; ProcessStartupInformation=$si}; "
    'Write-Output "$($r.ReturnValue) $($r.ProcessId)"'
)


def in_job(pid: int) -> bool | None:
    """kernel32 IsProcessInJob for pid; None when it cannot be determined.
    Callers treat None as "unknown" and assume the process is detached (only True triggers a fallback)."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            res = wintypes.BOOL()
            if not k.IsProcessInJob(h, None, ctypes.byref(res)):
                return None
            return bool(res.value)
        finally:
            k.CloseHandle(h)
    except (OSError, AttributeError, ImportError):
        return None


def host_port(host: str, port: int) -> str:
    """OLLAMA_HOST value; IPv6 literals are bracketed ([::1]:11434)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


_UNQUOTABLE = ('"', "%", "\r", "\n", "\x00")


def _wmi_cmdline(exe: str, host: str, port: int, log: Path, args: str = "serve") -> str:
    for part in (exe, str(log)):
        if any(c in part for c in _UNQUOTABLE):
            raise OSError(f"cannot quote {part!r} for cmd.exe")
    return f'cmd.exe /d /s /c "set OLLAMA_HOST={host_port(host, port)}&& "{exe}" {args} > "{log}" 2>&1"'


_WMI_RESULT = re.compile(r"(\d+)\s+(\d+)")


def _spawn_wmi(exe: str, host: str, port: int, log: Path, args: str = "serve") -> int:
    """Start `ollama serve` via WMI Win32_Process.Create (outside the host's job). Returns the cmd pid.
    `args` is internal (tests substitute a harmless command); it is never caller input."""
    env = dict(os.environ)
    env["MCP_CU_WMI_CMDLINE"] = _wmi_cmdline(exe, host, port, log, args)
    env["MCP_CU_WMI_CWD"] = str(Path(exe).parent)
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", _WMI_SCRIPT], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=WMI_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise OSError(f"WMI launcher timed out after {WMI_TIMEOUT}s") from e
    # PowerShell may print warnings first: take the last line that is exactly "<ret> <pid>".
    found = None
    for line in (proc.stdout or "").splitlines():
        m = _WMI_RESULT.fullmatch(line.strip())
        if m:
            found = m
    if proc.returncode != 0 or found is None or found.group(1) != "0":
        raise OSError(f"WMI Win32_Process.Create failed: {(proc.stdout or proc.stderr or '').strip()[:200]}")
    return int(found.group(2))


def _open_log(log_path: Path | None):
    if log_path is None:
        return subprocess.DEVNULL
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "ab")
    except OSError:
        return subprocess.DEVNULL


def _detach(proc) -> None:
    """Forget a child that must keep running: close our process handle (Windows) and stop
    Popen.__del__ from warning that it is still running. Never kills the child."""
    handle = getattr(proc, "_handle", None)
    if handle is not None and hasattr(handle, "Close"):
        try:
            handle.Close()
        except OSError:
            pass
    proc._child_created = False


def _spawn_impl(exe: str, host: str, port: int, log_path: Path | None = None) -> str:
    """Start `ollama serve` so it outlives this process. Returns the method used:
    "breakaway" / "wmi" (Windows, outside the host's job), "popen" (Windows last resort,
    may die with the host) or "setsid" (POSIX). With log_path None output goes to DEVNULL and
    the WMI step is skipped (it needs a log file), so Windows falls back to plain popen."""
    env = dict(os.environ)
    env["OLLAMA_HOST"] = host_port(host, port)  # child only
    kw: dict = {"stdin": subprocess.DEVNULL, "env": env, "close_fds": True}

    def popen(**extra):
        out = _open_log(log_path)
        try:
            return subprocess.Popen([exe, "serve"], stdout=out, stderr=out, **kw, **extra)
        finally:
            if out is not subprocess.DEVNULL:
                out.close()  # the child keeps its own handle

    if sys.platform != "win32":
        _detach(popen(start_new_session=True))
        return "setsid"
    flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    try:
        proc = popen(creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, startupinfo=si)
    except OSError:
        proc = None  # the host's job forbids breakaway
    if proc is not None:
        if in_job(proc.pid) is not True:
            _detach(proc)
            return "breakaway"
        # still in a job (e.g. a nested one that allowed breakaway): it would die with the host
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _spawn_wmi(exe, host, port, log_path)
            return "wmi"
        except (OSError, ValueError):
            pass
    _detach(popen(creationflags=flags, startupinfo=si))  # last resort: may be killed together with the host
    return "popen"


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
                method = _spawn(exe, host, port, log_path=sessions_dir(cfg) / LOG_NAME)
            except OSError as e:
                _release(lock)
                res["action"] = f"spawn_failed: {e}"
                return res
            res["started"] = True
            res["action"] = "started"
            res["method"] = state["method"] = method if isinstance(method, str) else None
        else:
            res["action"] = "waiting_for_other_start"  # another process is starting it
    else:
        res["action"] = "port_busy_waiting"  # something is listening; give it time
    try:
        # the readiness budget starts after the spawn returns: a slow WMI launch must not eat it
        deadline = clock() + cfg.autostart_timeout
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
