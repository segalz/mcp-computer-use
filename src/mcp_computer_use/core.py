"""Business logic for the MCP tools (kept free of MCP imports so it is easy to test)."""
from __future__ import annotations

import json
import time
from importlib import resources
from pathlib import Path

from . import agent_cu
from .config import Config
from .errors import CuError, APP_NOT_FOUND, QUESTION_TOO_LONG, EMPTY_QUESTION, error_payload
from .guidance import question_hints
from .extractors import extract, wants_snapshot
from .sessions import Session, sanitize
from .ollama_client import OllamaClient
from .timeouts import compute_timeout

RAW_FALLBACK_CHARS = 800  # raw text returned to the caller on model failure


def load_system_prompt(cfg: Config) -> str:
    if cfg.system_prompt_file:
        return Path(cfg.system_prompt_file).read_text(encoding="utf-8")
    return resources.files("mcp_computer_use").joinpath("prompts/system_prompt.md").read_text(encoding="utf-8")


HINT_SESSION = ("Tip: pass client (e.g. 'codex') and a session_id to look_at_screen to keep context "
                "between calls: shorter, more accurate answers and a 'changed_since_last' field.")
HINT_BAD_SESSION = "session_id had no valid characters [A-Za-z0-9_-]; this call ran without a session."

# client key -> monotonic time of the last call made WITHOUT session_id (only used for the hint)
_stateless_calls: dict[str, float] = {}


def build_user_message(question: str, app: str, status: str, text: str,
                       details: dict | None = None, changed: list[str] | None = None,
                       previous: list[dict] | None = None) -> str:
    msg = (f"<question>{question}</question>\n<app>{app}</app>\n"
           f"<detected_status>{status}</detected_status>\n")
    if details:
        msg += f"<details>{json.dumps(details, ensure_ascii=False)}</details>\n"
    if changed is not None:
        msg += f"<changed_since_last>{'; '.join(changed) or 'nothing changed'}</changed_since_last>\n"
    if previous:
        msg += f"<previous_questions>{json.dumps(previous, ensure_ascii=False)}</previous_questions>\n"
    return msg + f"<extracted>{text or '(empty)'}</extracted>"


def _stateless_hint(client: str | None, cfg: Config) -> bool:
    key = client or "-"
    now = time.monotonic()
    last = _stateless_calls.get(key)
    _stateless_calls[key] = now
    return last is not None and now - last <= cfg.stateless_hint_window


def look_at_screen(question: str, app: str | None, cfg: Config,
                   ollama: OllamaClient | None = None,
                   client: str | None = None, session_id: str | None = None) -> dict:
    t0 = time.monotonic()
    ollama = ollama or OllamaClient(cfg)
    elapsed = lambda: round(time.monotonic() - t0, 2)
    available: list[str] | None = None

    # 0) validate the question (callers should send one short English question)
    question = (question or "").strip()
    if not question:
        return error_payload(EMPTY_QUESTION, "question is empty", model_used=cfg.model, duration_seconds=elapsed())
    if len(question) > cfg.max_question_chars:
        return error_payload(QUESTION_TOO_LONG,
                             f"question is {len(question)} chars; limit is {cfg.max_question_chars}",
                             model_used=cfg.model, duration_seconds=elapsed())
    hints = question_hints(question)
    client_c, sid = sanitize(client), sanitize(session_id)
    if session_id and not sid:
        hints.append(HINT_BAD_SESSION)
    if not sid and _stateless_hint(client_c, cfg):
        hints.append(HINT_SESSION)
    hint = " ".join(hints) or None

    def done(d: dict) -> dict:
        if hint:
            d["hint"] = hint
        return d

    # 1) pick the window
    try:
        if app:
            target = app
        else:
            apps = agent_cu.list_apps(cfg.agent_cu_cmd, cfg.agent_cu_timeout)
            available = [a.name for a in apps]
            chosen = agent_cu.choose_app(apps, question)
            if not chosen:
                raise CuError(APP_NOT_FOUND, "agent-cu reported no open apps")
            target = chosen.name
        raw = agent_cu.window_text(target, cfg.agent_cu_cmd, cfg.agent_cu_timeout)
        snapshot = None
        if cfg.snapshot_details and wants_snapshot(target):
            try:
                snapshot = agent_cu.window_snapshot(target, cfg.agent_cu_cmd, cfg.agent_cu_timeout)
            except CuError:
                snapshot = None  # details are a bonus; the text answer still works
    except CuError as e:
        if e.code == APP_NOT_FOUND and available is None:
            try:
                available = [a.name for a in agent_cu.list_apps(cfg.agent_cu_cmd, cfg.agent_cu_timeout)]
            except CuError:
                pass
        return done(error_payload(e.code, e.message, available_apps=available,
                                  model_used=cfg.model, duration_seconds=elapsed()))

    # 2) code-based extraction (no model)
    ex = extract(raw, target, cfg.max_chars, cfg.generic_tail_chars, snapshot=snapshot)
    details = {k: v for k, v in ex.details.items() if v not in (None, "", [], False)}
    detected = ex.status
    if details.get("waiting_for_user") and detected != "working":
        detected = "waiting_for_user"
    extracted = {"status": detected, "last_message": ex.last_message[:RAW_FALLBACK_CHARS],
                 "extractor": ex.extractor, "truncated": ex.truncated or len(ex.last_message) > RAW_FALLBACK_CHARS}
    if details:
        extracted["details"] = details

    # 2b) optional session file: compare with the previous call
    session = None
    changed = previous = None
    if sid:
        try:
            session = Session.open(cfg, client_c, sid)
        except OSError as e:
            hint = ((hint + " ") if hint else "") + f"Session file unavailable ({e}); answered without session."
            session = None
    state = None
    if session:
        state = session.snapshot_state(target, detected, ex.last_message, details)
        changed = session.changes(state) if not session.is_new else None
        previous = session.recent_questions()
        cached = session.cached_answer(question, state["screen_hash"])
        if cached:
            session.save(state, question, cached)
            return done(_result(target, cached.get("status", detected), cached.get("summary", ""),
                                cached.get("confidence", 0.5), details, session, changed, cfg, elapsed(),
                                None, None, cached=True))

    # 3) local model summarises only the short text
    loaded = ollama.is_loaded()
    timeout = compute_timeout(len(ex.last_message), cfg, model_loaded=loaded)
    try:
        answer, stats = ollama.chat_json(load_system_prompt(cfg),
                                         build_user_message(question, target, detected, ex.last_message,
                                                            details, changed if session and not session.is_new else None,
                                                            previous),
                                         timeout)
    except CuError as e:
        return done(error_payload(e.code, e.message, windows_seen=[target], extracted=extracted,
                                  model_used=cfg.model, timeout_seconds=timeout, duration_seconds=elapsed()))

    status = answer.get("status") or detected
    if detected != "unknown" and status not in (detected, "waiting_for_user", "error"):
        status = detected  # code-detected status wins over a contradicting model
    try:
        confidence = max(0.0, min(1.0, float(answer.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    summary = str(answer.get("summary", "")).strip()
    if session and state is not None:
        try:
            session.save(state, question, {"status": status, "summary": summary, "confidence": round(confidence, 2)})
        except OSError:
            pass
    return done(_result(target, status, summary, confidence, details, session, changed, cfg, elapsed(),
                        ollama.model_memory_mb(), stats.get("tokens_per_second")))


def _result(target, status, summary, confidence, details, session, changed, cfg, duration,
            memory_mb, tps, cached=False) -> dict:
    d = {
        "windows_seen": [target],          # filled by code, never by the model
        "status": status,
        "summary": summary,
        "confidence": round(float(confidence), 2),
    }
    if details:
        d["details"] = details             # filled by code: project, session, next_step, failures...
    if session:
        d["changed_since_last"] = (["first call in this session"] if changed is None
                                   else (changed or ["nothing changed"]))
        d["session"] = {"client": session.client, "session_id": session.session_id}
        if session.replaced:
            d["session"]["replaced"] = session.replaced
        if cached:
            d["cached"] = True
    d.update({"model_used": cfg.model, "duration_seconds": duration, "memory_mb": memory_mb,
              "tokens_per_second": tps})
    return d


def health(cfg: Config, ollama: OllamaClient | None = None) -> dict:
    ollama = ollama or OllamaClient(cfg)
    out = {"ok": False, "ollama_url": cfg.ollama_url, "model": cfg.model,
           "ollama_reachable": False, "model_present": False, "model_loaded": False,
           "memory_mb": None, "agent_cu_available": False, "agent_cu_path": None, "problems": []}
    try:
        names = ollama.models()
        out["ollama_reachable"] = True
        from .ollama_client import _same_model
        out["model_present"] = any(_same_model(n, cfg.model) for n in names)
        if not out["model_present"]:
            out["problems"].append(f"model '{cfg.model}' not pulled (ollama pull {cfg.model})")
        out["model_loaded"] = ollama.is_loaded()
        out["memory_mb"] = ollama.model_memory_mb()
    except CuError as e:
        out["problems"].append(e.message)
    path = agent_cu.resolve_cmd(cfg.agent_cu_cmd)
    out["agent_cu_available"] = bool(path)
    out["agent_cu_path"] = path
    if not path:
        out["problems"].append(f"'{cfg.agent_cu_cmd}' not on PATH (npm i -g agent-cu)")
    out["ok"] = out["ollama_reachable"] and out["model_present"] and out["agent_cu_available"]
    return out
