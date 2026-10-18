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
    image_timeout: float = 15.0
    max_chars: int = 2000          # cap on extracted text sent to the model
    max_question_chars: int = 300  # longer questions are rejected (QUESTION_TOO_LONG)
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
    snapshot_details: bool = True   # extra `agent-cu snapshot` for project/session/branch (Claude)
    sessions_dir: str = ""          # default: <project>/.sessions (or ~/.mcp-computer-use/sessions)
    session_ttl_hours: float = 24.0  # session files not updated for longer are deleted
    keep_alive_idle: str = "3m"      # single call, no active session
    keep_alive_session: str = "10m"  # active session (file updated within session_active_minutes) or keep_warm
    session_active_minutes: float = 10.0
    min_free_mb: int = 2048          # free RAM below this -> unload right after answering (0 = off)
    memory_watch: bool = True        # background thread unloads under memory pressure
    memory_watch_interval: float = 30.0
    autostart_ollama: bool = True    # start a local `ollama serve` when the localhost URL is unreachable
    autostart_timeout: float = 10.0  # seconds to wait for /api/tags after starting
    stateless_hint_window: float = 900.0  # seconds; repeated calls without session_id get a hint
    safety_blocklist: str = ""
    safety_blocklist_extra: str = ""
    action_log_ttl_hours: float = 24.0
    resolve_with_model: bool = True
    max_type_chars: int = 2000


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
        raw_data = tomllib.loads(file_path.read_text(encoding="utf-8"))
        data = raw_data.get("mcp_computer_use", raw_data)
        for k, v in data.items():
            if k in types:
                if k in ("safety_blocklist", "safety_blocklist_extra") and isinstance(v, (list, tuple)):
                    setattr(cfg, k, ",".join(str(x).strip() for x in v))
                else:
                    setattr(cfg, k, _coerce(v, types[k]))

        safety_data: dict = {}
        if isinstance(raw_data.get("safety"), dict):
            safety_data.update(raw_data["safety"])
        if isinstance(raw_data.get("mcp_computer_use"), dict) and isinstance(raw_data["mcp_computer_use"].get("safety"), dict):
            safety_data.update(raw_data["mcp_computer_use"]["safety"])

        bl = safety_data.get("blocklist", safety_data.get("safety_blocklist"))
        if bl is None and "blocklist" in data and not isinstance(data.get("blocklist"), dict):
            bl = data["blocklist"]
        if bl is not None:
            cfg.safety_blocklist = ",".join(str(x).strip() for x in bl) if isinstance(bl, (list, tuple)) else str(bl)

        ble = safety_data.get("blocklist_extra", safety_data.get("safety_blocklist_extra"))
        if ble is None and "blocklist_extra" in data and not isinstance(data.get("blocklist_extra"), dict):
            ble = data["blocklist_extra"]
        if ble is not None:
            cfg.safety_blocklist_extra = ",".join(str(x).strip() for x in ble) if isinstance(ble, (list, tuple)) else str(ble)

        for field_name in ("action_log_ttl_hours", "resolve_with_model", "max_type_chars"):
            if field_name in safety_data:
                setattr(cfg, field_name, _coerce(safety_data[field_name], types[field_name]))

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
