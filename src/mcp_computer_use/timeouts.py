"""Model timeouts: a static or adaptive per-call timeout, capped by a total per-call wall budget.

Static (adaptive_timeout = false, or no warm samples yet):
    t = clamp(timeout_base + timeout_per_1k_chars * chars/1000, timeout_min, timeout_max)
Adaptive (warm samples recorded from earlier successful look_at_screen model calls):
    base = max(p90(last adaptive_window warm durations) * 2, timeout_min)
    t = clamp(base + timeout_per_1k_chars * chars/1000, timeout_min, timeout_max)
    The size term is added on top of the adaptive base. Samples already contain their own
    size cost, so this errs on the long side; it keeps a large window from inheriting the
    timeout learned on small ones.
After a MODEL_TIMEOUT the next adaptive timeout is multiplied by a bump (x1.5 per timeout,
result still capped by timeout_max); the next success resets the bump to 1.
Cold load (model not in /api/ps) adds cold_load_extra after the clamp. Finally the caller caps
the result with the call budget (budget_cap) so the whole tool call returns in time.
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from .config import Config

STATS_FILE = "timing_stats.json"  # in sessions_dir; no "__" so session cleanup never deletes it
BUMP_FACTOR = 1.5
MAX_BUMP = BUMP_FACTOR ** 3       # the timeout_max clamp caps the result anyway
MIN_MODEL_SECONDS = 5.0           # less than this left in the call budget: fail fast instead
_COLD_KEEP = 8                    # cold durations kept for information only


def compute_timeout(chars: int, cfg: Config, model_loaded: bool = True) -> float:
    """The static formula (also the adaptive fallback with no samples)."""
    t = cfg.timeout_base + cfg.timeout_per_1k_chars * (max(chars, 0) / 1000.0)
    t = min(max(t, cfg.timeout_min), cfg.timeout_max)
    if not model_loaded:
        t += cfg.cold_load_extra
    return round(t, 2)


def _p90(values: list[float]) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]  # nearest rank; with <= 10 samples this is the max


class TimingStats:
    """Rolling warm-call durations and the timeout bump, persisted as a small JSON file."""

    def __init__(self, path: Path | None, window: int = 8):
        self.path = path
        self.window = max(1, int(window))
        self.warm: list[float] = []
        self.cold: list[float] = []
        self.bump = 1.0
        self._lock = threading.Lock()

    def load(self) -> "TimingStats":
        """Read the file; a missing or corrupt file just means no history."""
        if not self.path:
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            warm = [float(x) for x in data.get("warm", []) if float(x) > 0]
            cold = [float(x) for x in data.get("cold", []) if float(x) > 0]
            bump = float(data.get("bump", 1.0))
        except (OSError, ValueError, TypeError, AttributeError):
            return self
        self.warm = warm[-self.window:]
        self.cold = cold[-_COLD_KEEP:]
        self.bump = min(max(bump, 1.0), MAX_BUMP) if math.isfinite(bump) else 1.0
        return self

    def save(self) -> None:
        """Atomic write (temp file + replace); failures are ignored (history is a bonus)."""
        if not self.path:
            return
        data = {"warm": [round(x, 2) for x in self.warm], "cold": [round(x, 2) for x in self.cold],
                "bump": round(self.bump, 4)}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def record_success(self, seconds: float, cold: bool) -> None:
        with self._lock:
            if cold:  # cold-load calls never become warm samples
                self.cold = (self.cold + [float(seconds)])[-_COLD_KEEP:]
            else:
                self.warm = (self.warm + [float(seconds)])[-self.window:]
            self.bump = 1.0
            self.save()

    def record_timeout(self) -> None:
        with self._lock:
            self.bump = min(self.bump * BUMP_FACTOR, MAX_BUMP)
            self.save()


_registry: dict[str, TimingStats] = {}
_registry_lock = threading.Lock()


def _stats_path(cfg: Config) -> Path:
    from .sessions import sessions_dir
    return sessions_dir(cfg) / STATS_FILE


def stats_for(cfg: Config) -> TimingStats:
    """One TimingStats per stats file per process, loaded from disk on first use."""
    path = _stats_path(cfg)
    with _registry_lock:
        st = _registry.get(str(path))
        if st is None or st.window != max(1, int(cfg.adaptive_window)):
            st = _registry[str(path)] = TimingStats(path, cfg.adaptive_window).load()
        return st


def model_timeout(chars: int, cfg: Config, model_loaded: bool = True,
                  stats: TimingStats | None = None) -> tuple[float, dict]:
    """Returns (timeout_seconds, info) with info = {timeout_source, samples}."""
    if not cfg.adaptive_timeout:
        return compute_timeout(chars, cfg, model_loaded), {"timeout_source": "static", "samples": 0}
    stats = stats if stats is not None else stats_for(cfg)
    warm = list(stats.warm)
    if not warm:
        t = compute_timeout(chars, cfg, True)
        source = "static"
    else:
        base = max(_p90(warm) * 2, cfg.timeout_min)
        t = base + cfg.timeout_per_1k_chars * (max(chars, 0) / 1000.0)
        t = min(max(t, cfg.timeout_min), cfg.timeout_max)
        source = "adaptive"
    if stats.bump > 1.0:
        t = min(t * stats.bump, cfg.timeout_max)
        source = "bumped"
    if not model_loaded:
        t += cfg.cold_load_extra
    return round(t, 2), {"timeout_source": source, "samples": len(warm)}


def budget_cap(timeout: float, cfg: Config, elapsed: float) -> tuple[float | None, bool]:
    """Cap a model timeout by what is left of call_budget_seconds after `elapsed` seconds.
    Returns (timeout, capped); timeout is None when less than MIN_MODEL_SECONDS remains."""
    if cfg.call_budget_seconds <= 0:
        return timeout, False
    remaining = cfg.call_budget_seconds - max(elapsed, 0.0)
    if remaining < MIN_MODEL_SECONDS:
        return None, True
    if timeout <= remaining:
        return timeout, False
    return round(remaining, 2), True


def budget_exhausted_message(cfg: Config, elapsed: float) -> str:
    return (f"call budget nearly used up before the model call ({elapsed:.1f}s of "
            f"call_budget_seconds={cfg.call_budget_seconds:g}s); retry: Ollama and the model are "
            f"likely warm now, so the next call should be fast")
