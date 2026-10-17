"""Configuration: defaults < config file (TOML) < environment variables."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ENV_PREFIX = "MCP_CU_"


@dataclass
class Config:
    ollama_url: str = "http://127.0.0.1:11435"
    model: str = "gpt-oss:20b"
    agent_cu_cmd: str = "agent-cu"
    agent_cu_timeout: float = 20.0
    max_chars: int = 2000          # cap on extracted text sent to the model
    generic_tail_chars: int = 2000  # generic extractor: last N chars of window text
    num_ctx: int = 4096
    temperature: float = 0.0
    # smart timeout (seconds) = base + per_1k_chars * (chars / 1000), clamped to [min, max]
    timeout_base: float = 10.0
    timeout_per_1k_chars: float = 4.0
    timeout_min: float = 8.0
    timeout_max: float = 60.0
    cold_load_extra: float = 45.0   # added when the model is not loaded (per /api/ps)
    health_http: bool = False
    health_host: str = "127.0.0.1"
    health_port: int = 8765
    system_prompt_file: str = ""    # optional override of the bundled prompt


# Short env aliases requested for convenience (in addition to MCP_CU_<FIELD>).
ALIASES = {"OLLAMA_URL": "ollama_url", "MODEL": "model"}


def _coerce(value, typ):
    if typ is bool or typ == "bool":
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if typ is int or typ == "int":
        return int(value)
    if typ is float or typ == "float":
        return float(value)
    return str(value)


def default_config_path() -> Path | None:
    p = os.environ.get(ENV_PREFIX + "CONFIG")
    if p:
        return Path(p)
    for cand in (Path.cwd() / "mcp-computer-use.toml",
                 Path.home() / ".config" / "mcp-computer-use" / "config.toml"):
        if cand.is_file():
            return cand
    return None


def load_config(path: str | Path | None = None, env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    types = {f.name: f.type for f in fields(Config)}
    file_path = Path(path) if path else default_config_path()
    if file_path and file_path.is_file():
        data = tomllib.loads(file_path.read_text(encoding="utf-8"))
        data = data.get("mcp_computer_use", data)
        for k, v in data.items():
            if k in types:
                setattr(cfg, k, _coerce(v, types[k]))
    for alias, name in ALIASES.items():
        if alias in env and env[alias]:
            setattr(cfg, name, _coerce(env[alias], types[name]))
    for name, typ in types.items():
        key = ENV_PREFIX + name.upper()
        if key in env and env[key] != "":
            setattr(cfg, name, _coerce(env[key], typ))
    cfg.ollama_url = cfg.ollama_url.rstrip("/")
    if not cfg.ollama_url.startswith("http"):
        cfg.ollama_url = "http://" + cfg.ollama_url
    return cfg
