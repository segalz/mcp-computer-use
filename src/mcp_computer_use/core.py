"""Business logic for the MCP tools (kept free of MCP imports so it is easy to test)."""
from __future__ import annotations

import json
import re
import time
from importlib import resources
from pathlib import Path

from . import agent_cu, matching, safety
from . import errors as action_errors
from .config import Config
from .errors import CuError, APP_NOT_FOUND, QUESTION_TOO_LONG, EMPTY_QUESTION, error_payload
from .guidance import question_hints
from .extractors import extract, wants_snapshot
from .sessions import Session, sanitize
from . import memory
from . import autostart
from .errors import OLLAMA_UNREACHABLE
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
                   client: str | None = None, session_id: str | None = None,
                   keep_warm: bool = False) -> dict:
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
    keep_alive, ka_reason = memory.keep_alive_for(cfg, session=session is not None, keep_warm=keep_warm)
    free_before = memory.free_ram_mb()
    # Ollama availability: checked on calls without a session and on the FIRST call of a session only.
    started = None
    if session is None or session.is_new:
        started = autostart.ensure_ollama(cfg, ollama)
    loaded = ollama.is_loaded()
    timeout = compute_timeout(len(ex.last_message), cfg, model_loaded=loaded)
    user_msg = build_user_message(question, target, detected, ex.last_message, details,
                                  changed if session and not session.is_new else None, previous)
    try:
        try:
            answer, stats = ollama.chat_json(load_system_prompt(cfg), user_msg, timeout, keep_alive=keep_alive)
        except CuError as e:
            # mid-session (or check skipped): auto-start once and retry once
            if e.code != OLLAMA_UNREACHABLE or started is not None:
                raise
            started = autostart.ensure_ollama(cfg, ollama)
            if not started.get("reachable"):
                raise
            loaded = ollama.is_loaded()
            timeout = compute_timeout(len(ex.last_message), cfg, model_loaded=loaded)
            answer, stats = ollama.chat_json(load_system_prompt(cfg), user_msg, timeout, keep_alive=keep_alive)
    except CuError as e:
        extra = {"ollama_autostart": started} if started and started.get("action") != "already_running" else {}
        return done(error_payload(e.code, e.message, windows_seen=[target], extracted=extracted,
                                  model_used=cfg.model, timeout_seconds=timeout, duration_seconds=elapsed(), **extra))
    mem_info = {"keep_alive": keep_alive, "reason": ka_reason, "was_loaded": loaded}
    if started and started.get("started"):
        mem_info["ollama_autostarted"] = True
        mem_info["ollama_start_seconds"] = started.get("seconds")
    memory_mb = ollama.model_memory_mb()
    free_after = memory.free_ram_mb()
    if memory.under_pressure(cfg, free_before) or memory.under_pressure(cfg, free_after):
        try:
            ollama.unload()
            mem_info["unloaded_low_memory"] = True
        except CuError:
            pass
    if free_after is not None:
        mem_info["free_ram_mb"] = free_after

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
    res = _result(target, status, summary, confidence, details, session, changed, cfg, elapsed(),
                  memory_mb, stats.get("tokens_per_second"))
    res["memory"] = mem_info
    return done(res)


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


def ping(cfg: Config, ollama: OllamaClient | None = None) -> dict:
    """Heartbeat: server up, local Ollama reachable (auto-start), model answers 'pong'. No screen reading."""
    t0 = time.monotonic()
    ollama = ollama or OllamaClient(cfg)
    out = {"ok": False, "server": True, "ollama": False, "model_reply_ok": False,
           "model": cfg.model, "seconds": 0.0, "cold": False}
    started = autostart.ensure_ollama(cfg, ollama)
    if started.get("started"):
        out["ollama_autostarted"] = True
    if not started.get("reachable"):
        out["error"] = f"Ollama at {cfg.ollama_url} unreachable (autostart: {started.get('action')})"
        out["seconds"] = round(time.monotonic() - t0, 2)
        return out
    out["ollama"] = True
    out["cold"] = not ollama.is_loaded()
    timeout = cfg.timeout_base + (cfg.cold_load_extra if out["cold"] else 0)
    try:
        reply = ollama.ping_model(timeout, keep_alive=cfg.keep_alive_idle)
        out["model_reply_ok"] = "pong" in reply.lower()
        if not out["model_reply_ok"]:
            out["error"] = f"unexpected model reply: {reply[:80]!r}"
    except CuError as e:
        out["error"] = f"{e.code}: {e.message}"
    out["ok"] = out["model_reply_ok"]
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


def release_model(cfg: Config, ollama: OllamaClient | None = None) -> dict:
    return memory.release(cfg, ollama or OllamaClient(cfg))


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
    out["ollama_autostarted"] = autostart.state["autostarted"]
    out["autostart_enabled"] = bool(cfg.autostart_ollama and autostart.target(cfg))
    if autostart.state["last"]:
        out["last_autostart"] = autostart.state["last"]
    free = memory.free_ram_mb()
    current, reason = memory.keep_alive_for(cfg)
    out["memory"] = {"free_ram_mb": free, "min_free_mb": cfg.min_free_mb,
                     "under_pressure": memory.under_pressure(cfg, free) if free is not None else None,
                     "keep_alive_idle": cfg.keep_alive_idle, "keep_alive_session": cfg.keep_alive_session,
                     "keep_alive_next_call": current, "policy_reason": reason,
                     "memory_watch": cfg.memory_watch}
    path = agent_cu.resolve_cmd(cfg.agent_cu_cmd)
    out["agent_cu_available"] = bool(path)
    out["agent_cu_path"] = path
    if not path:
        out["problems"].append(f"'{cfg.agent_cu_cmd}' not on PATH (npm i -g agent-cu)")
    out["ok"] = out["ollama_reachable"] and out["model_present"] and out["agent_cu_available"]
    return out


_EDITABLE_ROLES = {"textfield", "textarea", "edit", "combobox", "textbox", "document", "searchfield"}
_ACTION_CODES = {
    action_errors.AGENT_CU_FAILED, action_errors.AGENT_CU_NOT_INSTALLED,
    APP_NOT_FOUND, action_errors.INVALID_TARGET, action_errors.TEXT_TOO_LONG,
    action_errors.TARGET_NOT_FOUND, action_errors.TARGET_AMBIGUOUS,
    action_errors.TARGET_NOT_EDITABLE, action_errors.DANGEROUS_ACTION_NEEDS_CONFIRMATION,
}


def _action_element(element: agent_cu.Element, context: bool = False) -> dict:
    result = {"ref": element.ref[:40], "name": element.name[:80], "role": element.role[:32]}
    if context:
        result["context"] = element.context[:40]
    return result


def _action_result(result: dict, text: str | None) -> dict:
    """Apply the audit logger's text-scrubbing policy to result strings as well."""
    needles = safety._build_needles(text) if text else set()

    def scrub(value):
        if isinstance(value, str):
            return safety._scrub_value(value, needles)
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    result = scrub(result)
    # Use the larger ASCII-escaped encoding as the budget, including JSON spaces.
    for _ in range(16):
        if len(json.dumps(result, indent=2)) < 1500:
            return result
        if len(result.get("candidates", [])) > 1:
            result["candidates"].pop()
            continue
        if "hint" in result:
            result.pop("hint")
            continue

        def shorten(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"app", "name", "context", "ref", "role", "matched_rule"} and isinstance(item, str):
                        value[key] = item[:len(item) // 2]
                    elif isinstance(item, (list, dict)):
                        shorten(item)
            elif isinstance(value, list):
                for item in value:
                    shorten(item)
        shorten(result)
    # Defensive fallback for unexpected, untrimmable fields; preserve whether
    # the action completed so an already-executed action is not reported failed.
    fallback = {"ok": result.get("ok") is True,
                "action": "type" if result.get("action") == "type" else "click",
                "app": "", "hint": "Action response details omitted."}
    if not fallback["ok"]:
        code = result.get("error", {}).get("code")
        code = code if code in _ACTION_CODES else action_errors.AGENT_CU_FAILED
        fallback.update(error_payload(code, "Action response details omitted."))
    return scrub(fallback)


def _action(action: str, app: str, target: str, cfg: Config, text: str | None = None,
            ollama: OllamaClient | None = None, client: str | None = None,
            session_id: str | None = None, confirm_dangerous: bool = False) -> dict:
    t0 = time.monotonic()
    selected = None
    method = None
    confirmed = confirm_dangerous is True
    private_text = text if action == "type" and isinstance(text, str) else None
    client_c, sid = sanitize(client), sanitize(session_id)

    def finish(state: str, code: str | None = None, message: str = "", **extra) -> dict:
        result = {"ok": code is None, "action": action,
                  "app": app[:80] if isinstance(app, str) else "",
                  "duration_seconds": round(time.monotonic() - t0, 2)}
        if code:
            result.update(error_payload(code, message))
        else:
            result["clicked" if action == "click" else "typed_into"] = _action_element(selected)
            result["match"] = method
            if action == "type":
                result["chars"] = len(text)
        result.update(extra)
        try:
            hint = safety.log_action(
                cfg, text=private_text, client=client_c, session_id=sid, app=app,
                action=action, target=target, ref=selected.ref if selected else None,
                name=selected.name if selected else None, role=selected.role if selected else None,
                result=state, confirmed=confirmed, error_code=code,
                text_len=len(private_text) if private_text is not None else None,
            )
        except Exception:
            hint = "Action log unavailable."
        if hint:
            result["hint"] = str(hint)[:160]
        return _action_result(result, private_text)

    def blocked(rule: str) -> dict:
        would_click = (_action_element(selected) if selected else
                       {"ref": None, "name": target[:80], "role": None})
        return finish("blocked", action_errors.DANGEROUS_ACTION_NEEDS_CONFIRMATION,
                      "This action needs explicit human confirmation.",
                      would_click=would_click, matched_rule=rule[:80])

    if not isinstance(target, str) or not target.strip() or len(target) > 200 or re.fullmatch(r"@e\d+", target.strip()):
        return finish("error", action_errors.INVALID_TARGET, "Pass a visible label of 1 to 200 characters, not a ref.")
    if not isinstance(app, str) or not app.strip() or re.search(r'[&|<>^%"\r\n\x00]', app):
        return finish("error", APP_NOT_FOUND, "Pass a non-empty app name without shell metacharacters.")
    if action == "type":
        if not isinstance(text, str):
            return finish("error", action_errors.INVALID_TARGET, "Text must be a string.")
        if len(text) > cfg.max_type_chars:
            return finish("error", action_errors.TEXT_TOO_LONG, "Text exceeds max_type_chars.")
    try:
        words = safety.blocklist(cfg)
        rule = safety.is_dangerous(target, "", words)
        if rule and not confirmed:
            return blocked(rule)
        snapshot = agent_cu.snapshot_interactive(app, cfg.agent_cu_cmd, cfg.agent_cu_timeout)
        elements = agent_cu.parse_elements(snapshot)
        resolved = matching.resolve(target, elements, role_pref=_EDITABLE_ROLES if action == "type" else None)
        if isinstance(resolved, matching.NoMatch):
            if action == "type":
                # Diagnose an excluded label; it is never used as an action target.
                excluded = matching.resolve(target, elements)
                if (isinstance(excluded, matching.Ambiguous) and excluded.candidates
                        and all(element.role not in _EDITABLE_ROLES for element in excluded.candidates)):
                    return finish("error", action_errors.TARGET_NOT_EDITABLE,
                                  "The matching targets are not editable fields.",
                                  candidates=[_action_element(element, True) for element in excluded.candidates[:5]])
                if isinstance(excluded, matching.Match) and excluded.element.role not in _EDITABLE_ROLES:
                    selected = excluded.element
                    rule = safety.is_dangerous("", selected.name, words)
                    if rule and not confirmed:
                        return blocked(rule)
                    return finish("error", action_errors.TARGET_NOT_EDITABLE, "The target is not an editable field.")
            return finish("not_found", action_errors.TARGET_NOT_FOUND, "No matching target was found.",
                          candidates=[_action_element(element, True) for element in resolved.near[:5]])
        if isinstance(resolved, matching.Ambiguous):
            candidates = resolved.candidates[:5]
            picked = None
            if cfg.resolve_with_model:
                try:
                    picked = matching.pick_with_model(target, candidates, ollama or OllamaClient(cfg), cfg)
                except Exception:
                    picked = None
            selected = next((element for element in candidates
                             if isinstance(picked, agent_cu.Element) and picked.ref == element.ref), None)
            if selected is None:
                return finish("ambiguous", action_errors.TARGET_AMBIGUOUS, "Choose one of the candidate labels.",
                              candidates=[_action_element(element, True) for element in candidates])
            method = "model"
        else:
            selected, method = resolved.element, resolved.how
        rule = safety.is_dangerous("", selected.name, words)
        if rule and not confirmed:
            return blocked(rule)
        if action == "type" and selected.role not in _EDITABLE_ROLES:
            return finish("error", action_errors.TARGET_NOT_EDITABLE, "The target is not an editable field.")
        if action == "click":
            agent_cu.click(selected.ref, app, cfg.agent_cu_cmd, cfg.agent_cu_timeout)
        else:
            agent_cu.type_text(text, app, selected.ref, cfg.agent_cu_cmd, cfg.agent_cu_timeout)
    except CuError as error:
        code = error.code if error.code in _ACTION_CODES else action_errors.AGENT_CU_FAILED
        return finish("error", code, "The action could not complete.")
    except Exception:
        # Action tools return structured failures without forwarding diagnostics.
        return finish("error", action_errors.AGENT_CU_FAILED, "The action could not complete.")
    return finish("done")


def click(app: str, target: str, cfg: Config, ollama: OllamaClient | None = None,
          client: str | None = None, session_id: str | None = None,
          confirm_dangerous: bool = False) -> dict:
    return _action("click", app, target, cfg, ollama=ollama, client=client,
                   session_id=session_id, confirm_dangerous=confirm_dangerous)


def type_text(app: str, target: str, text: str, cfg: Config, ollama: OllamaClient | None = None,
              client: str | None = None, session_id: str | None = None,
              confirm_dangerous: bool = False) -> dict:
    return _action("type", app, target, cfg, text=text, ollama=ollama, client=client,
                   session_id=session_id, confirm_dangerous=confirm_dangerous)
