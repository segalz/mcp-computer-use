"""Smart timeout that scales with the amount of text the model must read."""
from __future__ import annotations

from .config import Config


def compute_timeout(chars: int, cfg: Config, model_loaded: bool = True) -> float:
    t = cfg.timeout_base + cfg.timeout_per_1k_chars * (max(chars, 0) / 1000.0)
    t = min(max(t, cfg.timeout_min), cfg.timeout_max)
    if not model_loaded:
        t += cfg.cold_load_extra
    return round(t, 2)
