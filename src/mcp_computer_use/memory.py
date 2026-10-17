"""Smart model memory release: keep_alive policy, free-RAM checks, unload, background watcher."""
from __future__ import annotations

import ctypes
import sys
import threading
import time

from .config import Config
from .sessions import sessions_dir


def free_ram_mb() -> float | None:
    """Available system RAM in MB, or None if it cannot be read."""
    try:
        import psutil  # optional
        return round(psutil.virtual_memory().available / 2**20, 1)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullAvailPhys / 2**20, 1)
        except Exception:
            return None
        return None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        return None
    return None


def active_session(cfg: Config, now: float | None = None) -> bool:
    """True if any session file was updated within session_active_minutes."""
    folder = sessions_dir(cfg)
    if not folder.is_dir():
        return False
    limit = (now or time.time()) - cfg.session_active_minutes * 60
    for f in folder.glob("*__*.json"):
        try:
            if f.stat().st_mtime >= limit:
                return True
        except OSError:
            pass
    return False


def keep_alive_for(cfg: Config, session: bool = False, keep_warm: bool = False) -> tuple[str, str]:
    """(keep_alive value, reason). session=True means this call itself uses a session."""
    if session:
        return cfg.keep_alive_session, "session"
    if keep_warm:
        return cfg.keep_alive_session, "keep_warm"
    if active_session(cfg):
        return cfg.keep_alive_session, "active_session"
    return cfg.keep_alive_idle, "single_call"


def under_pressure(cfg: Config, free: float | None = None) -> bool:
    free = free_ram_mb() if free is None else free
    return free is not None and cfg.min_free_mb > 0 and free < cfg.min_free_mb


def release(cfg: Config, ollama) -> dict:
    """Unload the model now; returns freed memory."""
    out = {"released": False, "was_loaded": False, "model": cfg.model, "freed_mb": 0.0}
    try:
        before = ollama.model_memory_mb()
        was_loaded = any(_same(m, cfg.model) for m in ollama.running())
        out["was_loaded"] = was_loaded
        if was_loaded:
            ollama.unload()
            still = any(_same(m, cfg.model) for m in ollama.running())
            out["released"] = not still
            out["freed_mb"] = (before or 0.0) if not still else 0.0
        else:
            out["released"] = True  # nothing to free
    except Exception as e:  # CuError / transport errors: report, never raise
        out["error"] = str(e)
    out["free_ram_mb"] = free_ram_mb()
    return out


def _same(m: dict, model: str) -> bool:
    from .ollama_client import _same_model
    return _same_model(m.get("name") or m.get("model", ""), model)


class MemoryWatcher:
    """Daemon thread: every `interval` s, if the model is loaded and free RAM is low, unload it."""

    def __init__(self, cfg: Config, ollama, interval: float | None = None):
        self.cfg, self.ollama = cfg, ollama
        self.interval = interval or cfg.memory_watch_interval
        self._stop = threading.Event()
        self.unloads = 0
        self.thread = threading.Thread(target=self._run, name="mcp-cu-memory-watch", daemon=True)

    def start(self) -> "MemoryWatcher":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def check_once(self) -> bool:
        try:
            if self.ollama.is_loaded() and under_pressure(self.cfg):
                self.ollama.unload()
                self.unloads += 1
                return True
        except Exception:
            pass
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.check_once()
