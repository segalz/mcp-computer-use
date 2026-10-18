"""Mocked end-to-end action pipeline, privacy, safety, and CLI contracts."""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from mcp_computer_use import agent_cu, autostart, core, matching, safety, server
from mcp_computer_use.errors import CuError, AGENT_CU_FAILED, APP_NOT_FOUND
from mcp_computer_use.sessions import sessions_dir
from conftest import make_ollama


@pytest.fixture(autouse=True)
def no_action_model_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unmocked Ollama client in an action test")
    monkeypatch.setattr(core, "OllamaClient", unexpected)


def records(cfg):
    return [json.loads(line) for line in (sessions_dir(cfg) / "actions.log").read_text(encoding="utf-8").splitlines()]


def snapshot(fake_cu, contents, app="Editor"):
    fake_cu.snaps[app] = contents


def test_click_hebrew_calculator_and_log(cfg, fake_cu):
    fake_cu.snaps["Calculator"] = (Path(__file__).parent / "fixtures/snapshots/calculator_he.txt").read_text(encoding="utf-8")
    result = core.click("Calculator", "7", cfg, client="co.dex", session_id="a/b")
    assert result == {"ok": True, "action": "click", "app": "Calculator",
                      "clicked": {"ref": "@e32", "name": "שבע", "role": "button"},
                      "match": "synonym", "duration_seconds": result["duration_seconds"]}
    assert fake_cu.clicked == {"Calculator": ["@e32"]}
    assert fake_cu.calls == [["snapshot", "-a", "Calculator", "-i", "-c", "--human"],
                             ["click", "@e32", "-a", "Calculator"]]
    record = records(cfg)[0]
    assert record["result"] == "done" and record["confirmed"] is False
    assert record["client"] == "codex" and record["session_id"] == "ab"
    assert record["ref"] == "@e32"
    assert not list(sessions_dir(cfg).glob("*__*.json"))


def test_type_happy_path(cfg, fake_cu):
    snapshot(fake_cu, '[@e4] textfield "Body"')
    result = core.type_text("Editor", "Body", "hello\r\nשלום", cfg)
    assert result["ok"] and result["action"] == "type" and result["chars"] == 11
    assert result["typed_into"] == {"ref": "@e4", "name": "Body", "role": "textfield"}
    assert fake_cu.typed == {"Editor": [("@e4", "hello\r\nשלום")]}
    assert records(cfg)[0]["text_len"] == 11
    assert records(cfg)[0]["result"] == "done"


@pytest.mark.parametrize("target, name", [("Close", "Close"), ("Save", "Save changes"), ("Equls", "Equals")])
def test_ambiguous_without_model_never_acts(cfg, fake_cu, target, name):
    cfg.resolve_with_model = False
    snapshot(fake_cu, f'[@e1] button "{name}"\n[@e2] button "{name}"')
    result = core.click("Editor", target, cfg)
    assert result["error"]["code"] == "TARGET_AMBIGUOUS"
    assert not result["ok"] and len(result["candidates"]) == 2
    assert records(cfg)[0]["result"] == "ambiguous"
    assert not fake_cu.clicked and not fake_cu.typed


def test_single_fuzzy_needs_model(cfg, fake_cu):
    cfg.resolve_with_model = False
    snapshot(fake_cu, '[@e1] button "Equals"')
    result = core.click("Editor", "Equls", cfg)
    assert result["error"]["code"] == "TARGET_AMBIGUOUS"
    assert not fake_cu.clicked


def test_fake_model_pick_runs_and_never_autostarts(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Close"\n[@e2] button "Close"')
    def no_autostart(*args, **kwargs):
        raise AssertionError("actions must not autostart Ollama")
    monkeypatch.setattr(autostart, "ensure_ollama", no_autostart)
    sent = []
    result = core.click("Editor", "Close", cfg, make_ollama(cfg, chat={"ref": "@e2"}, record=sent))
    assert result["ok"] and result["match"] == "model"
    assert fake_cu.clicked == {"Editor": ["@e2"]}
    assert len(sent) == 1


@pytest.mark.parametrize("mode", ["null", "invalid", "timeout", "unreachable"])
def test_model_failure_returns_candidates(cfg, fake_cu, mode):
    snapshot(fake_cu, '[@e1] button "Close"\n[@e2] button "Close"')
    def timeout(request, body):
        raise httpx.ReadTimeout("private diagnostic", request=request)
    ollama = make_ollama(cfg, chat=timeout if mode == "timeout" else {"ref": None if mode == "null" else "@e999"},
                         unreachable=mode == "unreachable")
    result = core.click("Editor", "Close", cfg, ollama)
    assert result["error"]["code"] == "TARGET_AMBIGUOUS"
    assert len(result["candidates"]) == 2 and not fake_cu.clicked
    assert records(cfg)[0]["result"] == "ambiguous"


@pytest.mark.parametrize("target", ["Delete", "Send message", "שלח", "ושלח", "הסר התקנה"])
@pytest.mark.parametrize("action", ["click", "type"])
def test_blocked_target_precedes_snapshot(cfg, fake_cu, target, action):
    result = (core.click("Editor", target, cfg) if action == "click" else
              core.type_text("Editor", target, "private", cfg))
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert result["matched_rule"]
    assert result["would_click"]["ref"] is None
    assert not fake_cu.clicked and not fake_cu.typed
    assert not fake_cu.calls
    assert records(cfg)[0]["result"] == "blocked"


@pytest.mark.parametrize("action", ["click", "type"])
def test_blocked_element_name_after_harmless_target(cfg, fake_cu, action):
    role = "button" if action == "click" else "textfield"
    snapshot(fake_cu, f'[@e1] {role} "Delete file" id=OK')
    result = (core.click("Editor", "OK", cfg) if action == "click" else
              core.type_text("Editor", "OK", "private", cfg))
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert result["would_click"] == {"ref": "@e1", "name": "Delete file", "role": role}
    assert not fake_cu.clicked and not fake_cu.typed
    assert records(cfg)[0]["result"] == "blocked"


def test_model_pick_gets_name_safety_check(cfg, fake_cu):
    snapshot(fake_cu, '[@e1] button "Delete file" id=OK\n[@e2] button "Delete folder" id=OK')
    result = core.click("Editor", "OK", cfg, make_ollama(cfg, chat={"ref": "@e2"}))
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert result["would_click"]["ref"] == "@e2"
    assert not fake_cu.clicked and records(cfg)[0]["result"] == "blocked"


@pytest.mark.parametrize("action", ["click", "type"])
def test_confirmed_action_executes_and_logs(cfg, fake_cu, action):
    role = "button" if action == "click" else "textfield"
    snapshot(fake_cu, f'[@e1] {role} "Delete file"')
    result = (core.click("Editor", "Delete file", cfg, confirm_dangerous=True) if action == "click" else
              core.type_text("Editor", "Delete file", "private", cfg, confirm_dangerous=True))
    assert result["ok"]
    record = records(cfg)[0]
    assert record["result"] == "done" and record["confirmed"] is True
    assert len(fake_cu.calls) == 2
    if action == "click":
        assert fake_cu.clicked == {"Editor": ["@e1"]} and not fake_cu.typed
    else:
        assert fake_cu.typed == {"Editor": [("@e1", "private")]} and not fake_cu.clicked


@pytest.mark.parametrize("target", ["", "  ", "x" * 201, "@e5", " @e5 "])
@pytest.mark.parametrize("action", ["click", "type"])
def test_invalid_target_before_io(cfg, fake_cu, target, action):
    result = (core.click("Editor", target, cfg) if action == "click" else
              core.type_text("Editor", target, "private", cfg))
    assert result["error"]["code"] == "INVALID_TARGET"
    assert not fake_cu.calls
    assert records(cfg)[0]["result"] == "error"


def test_text_limit_before_snapshot(cfg, fake_cu):
    cfg.max_type_chars = 3
    result = core.type_text("Editor", "Body", "four", cfg)
    assert result["error"]["code"] == "TEXT_TOO_LONG" and not fake_cu.calls
    assert records(cfg)[0]["text_len"] == 4


def test_type_noneditable_target(cfg, fake_cu):
    snapshot(fake_cu, '[@e1] button "Body"')
    result = core.type_text("Editor", "Body", "private", cfg)
    assert result["error"]["code"] == "TARGET_NOT_EDITABLE"
    assert not fake_cu.typed and not fake_cu.clicked
    assert records(cfg)[0]["result"] == "error"


def test_type_hard_role_filter_selects_editable_label(cfg, fake_cu):
    snapshot(fake_cu, '[@e1] button "Body"\n[@e2] document "Body"')
    result = core.type_text("Editor", "Body", "private", cfg)
    assert result["ok"] and result["typed_into"]["ref"] == "@e2"
    assert fake_cu.typed == {"Editor": [("@e2", "private")]}


def test_not_found_candidates_and_audit(cfg, fake_cu):
    snapshot(fake_cu, '[@e1] button "axcd"\n[@e2] button "abef"')
    result = core.click("Editor", "abcd", cfg)
    assert result["error"]["code"] == "TARGET_NOT_FOUND"
    assert len(result["candidates"]) == 2
    assert records(cfg)[0]["result"] == "not_found" and not fake_cu.clicked


@pytest.mark.parametrize("failure, code", [("timeout", "AGENT_CU_FAILED"), ("error", "AGENT_CU_FAILED"), ("missing", "AGENT_CU_NOT_INSTALLED")])
def test_snapshot_failure_is_logged(cfg, fake_cu, monkeypatch, failure, code):
    fake_cu.fail = failure
    if failure == "missing":
        monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: None)
    result = core.click("Editor", "Body", cfg)
    assert result["error"]["code"] == code
    assert records(cfg)[0]["result"] == "error" and not fake_cu.clicked


SENTINELS = ["SENTINEL-7f3a", "line1\r\nSENTINEL-B", "סודי-SENTINEL", 'private-"quoted"-SENTINEL\r\nvalue']


def assert_private(result, cfg, text):
    log = (sessions_dir(cfg) / "actions.log").read_text(encoding="utf-8")
    payloads = [json.dumps(result, ensure_ascii=True), json.dumps(result, ensure_ascii=False), log]
    forms = [text, json.dumps(text, ensure_ascii=True)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]]
    for rendered in payloads:
        for form in forms:
            assert form not in rendered
            assert all(form[index:index + 6] not in rendered for index in range(len(form) - 5))


@pytest.mark.parametrize("text", SENTINELS)
@pytest.mark.parametrize("mode", ["done", "blocked_target", "blocked_name", "invalid", "too_long", "not_found", "ambiguous",
                                  "not_editable", "snapshot_error", "exit", "json_exit", "truncated", "timeout", "os_error", "value_error", "unexpected_error"])
def test_typed_sentinel_never_in_result_or_audit(cfg, fake_cu, monkeypatch, text, mode):
    cfg.resolve_with_model = False
    target = "Body"
    snapshot(fake_cu, '[@e1] textfield "Body"')
    expected = {"done": None, "blocked_target": "DANGEROUS_ACTION_NEEDS_CONFIRMATION",
                "blocked_name": "DANGEROUS_ACTION_NEEDS_CONFIRMATION", "invalid": "INVALID_TARGET",
                "too_long": "TEXT_TOO_LONG", "not_found": "TARGET_NOT_FOUND", "ambiguous": "TARGET_AMBIGUOUS",
                "not_editable": "TARGET_NOT_EDITABLE"}.get(mode, "AGENT_CU_FAILED")
    if mode == "blocked_target":
        target = "Delete"
    elif mode == "blocked_name":
        snapshot(fake_cu, '[@e1] textfield "Delete file" id=Body')
    elif mode == "invalid":
        target = ""
    elif mode == "too_long":
        cfg.max_type_chars = 1
    elif mode == "not_found":
        snapshot(fake_cu, '[@e1] textfield "zzzz"')
    elif mode == "ambiguous":
        snapshot(fake_cu, '[@e1] textfield "Body"\n[@e2] textfield "Body"')
    elif mode == "not_editable":
        snapshot(fake_cu, '[@e1] button "Body"')
    elif mode == "snapshot_error":
        def fail(*args, **kwargs):
            raise CuError(AGENT_CU_FAILED, text)
        monkeypatch.setattr(agent_cu, "snapshot_interactive", fail)
    elif mode in {"exit", "json_exit", "truncated", "timeout", "os_error", "value_error", "unexpected_error"}:
        def fail(args, **kwargs):
            if args[1] != "type":
                return fake_cu(args, **kwargs)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=text, stderr=text)
            if mode == "os_error":
                raise OSError(text)
            if mode == "value_error":
                raise ValueError(text)
            if mode == "unexpected_error":
                raise RuntimeError(text)
            stderr = text
            if mode in {"json_exit", "truncated"}:
                stderr = json.dumps({"type": "element_not_found", "message": text})
                if mode == "truncated":
                    stderr = stderr[:-4]
            return subprocess.CompletedProcess(args, 1, text, stderr)
        monkeypatch.setattr(agent_cu.subprocess, "run", fail)
    result = core.type_text("Editor", target, text, cfg)
    if expected:
        assert result["error"]["code"] == expected
    else:
        assert result["ok"]
    assert_private(result, cfg, text)
    assert len(records(cfg)) == 1


@pytest.mark.parametrize("text", SENTINELS)
def test_metadata_and_hint_echoes_are_scrubbed(cfg, fake_cu, monkeypatch, text):
    # Live field labels can already echo the text before this action starts.
    name = "Body " + text.replace('"', "'").replace("\r", " ").replace("\n", " ")
    snapshot(fake_cu, f'[@e1] textfield "{name}" id=Body')
    original = safety.log_action
    def hint(*args, **kwargs):
        original(*args, **kwargs)
        return text
    monkeypatch.setattr(safety, "log_action", hint)
    result = core.type_text("Editor", "Body", text, cfg, client=text, session_id=text)
    assert result["ok"]
    assert_private(result, cfg, text)


@pytest.mark.parametrize("mode", ["ambiguous", "not_found", "blocked", "done"])
def test_result_size_with_500_element_snapshot(cfg, fake_cu, mode):
    cfg.resolve_with_model = False
    label = "ש" * 100
    snapshot(fake_cu, 'window "' + "ק" * 100 + '"\n' + "\n".join(
        f'  [@e{i}] button "{label}" id=unique_{i}' for i in range(1, 501)))
    target = {"ambiguous": label, "not_found": "ש" * 40 + "ב" * 60,
              "blocked": "Delete", "done": "unique_500"}[mode]
    result = core.click("Editor", target, cfg)
    assert len(json.dumps(result)) < 1500
    assert len(json.dumps(result, ensure_ascii=False)) < 1500
    assert len(json.dumps(result, indent=2)) < 1500
    assert len(result.get("candidates", [])) <= 5
    assert "snapshot" not in result


def test_action_log_failure_adds_hint_without_failing_action(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Seven"')
    monkeypatch.setattr(safety, "log_action", lambda *args, **kwargs: "action log skipped: lock timeout")
    result = core.click("Editor", "Seven", cfg)
    assert result["ok"] and result["hint"] == "action log skipped: lock timeout"
    assert fake_cu.clicked


@pytest.mark.parametrize("action", ["click", "type"])
def test_cli_dispatch_and_confirmation(cfg, fake_cu, monkeypatch, capsys, action):
    monkeypatch.setattr(server, "load_config", lambda path: cfg)
    role = "button" if action == "click" else "textfield"
    snapshot(fake_cu, f'[@e1] {role} "Delete file"')
    args = ["--" + action, "Editor", "Delete file"]
    if action == "type":
        args.append("SENTINEL-7f3a")
    server.main(args + ["--confirm-dangerous", "--client", "codex", "--session-id", "cli"])
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["action"] == action
    record = records(cfg)[0]
    assert record["confirmed"] is True and record["client"] == "codex" and record["session_id"] == "cli"
    assert "SENTINEL-7f3a" not in json.dumps(result)


def test_cli_default_does_not_confirm(cfg, fake_cu, monkeypatch, capsys):
    monkeypatch.setattr(server, "load_config", lambda path: cfg)
    server.main(["--click", "Editor", "Delete"])
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert not fake_cu.calls


@pytest.mark.parametrize("action", ["click", "type_text"])
def test_mcp_tool_dispatches(cfg, fake_cu, action):
    snapshot(fake_cu, '[@e1] textfield "Body"')
    mcp = server.build_server(cfg)
    args = {"app": "Editor", "target": "Body", "client": "codex", "session_id": "mcp"}
    if action == "type_text":
        args["text"] = "SENTINEL-7f3a"
    response = asyncio.run(mcp.call_tool(action, args))
    rendered = str(response)
    assert "SENTINEL-7f3a" not in rendered
    record = records(cfg)[0]
    assert record["result"] == "done" and record["session_id"] == "mcp"
    assert fake_cu.clicked if action == "click" else fake_cu.typed


@pytest.mark.parametrize("confirmation", [False, "false", "true", 1, None])
def test_core_confirmation_requires_boolean_true(cfg, fake_cu, confirmation):
    result = core.click("Editor", "Delete", cfg, confirm_dangerous=confirmation)
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert not fake_cu.calls and records(cfg)[0]["confirmed"] is False


def test_model_cannot_replace_candidate_metadata(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Delete file" id=OK\n[@e2] button "Delete folder" id=OK')
    monkeypatch.setattr(matching, "pick_with_model", lambda *args: agent_cu.Element("@e1", "button", "Harmless"))
    result = core.click("Editor", "OK", cfg, make_ollama(cfg))
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert result["would_click"]["name"] == "Delete file" and not fake_cu.clicked


def test_unlisted_model_pick_stays_ambiguous(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Close"\n[@e2] button "Close"')
    monkeypatch.setattr(matching, "pick_with_model", lambda *args: agent_cu.Element("@e999", "button", "Close"))
    result = core.click("Editor", "Close", cfg, make_ollama(cfg))
    assert result["error"]["code"] == "TARGET_AMBIGUOUS" and not fake_cu.clicked


def test_type_model_pick_is_still_checked_for_editability(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Body"')
    button = agent_cu.Element("@e1", "button", "Body")
    monkeypatch.setattr(matching, "resolve", lambda *args, **kwargs: matching.Ambiguous([button]))
    monkeypatch.setattr(matching, "pick_with_model", lambda *args: button)
    result = core.type_text("Editor", "Body", "private", cfg, make_ollama(cfg))
    assert result["error"]["code"] == "TARGET_NOT_EDITABLE" and not fake_cu.typed


def test_type_diagnosed_noneditable_element_still_gets_safety_check(cfg, fake_cu):
    snapshot(fake_cu, '[@e1] button "Delete file" id=OK')
    result = core.type_text("Editor", "OK", "private", cfg)
    assert result["error"]["code"] == "DANGEROUS_ACTION_NEEDS_CONFIRMATION"
    assert not fake_cu.clicked and not fake_cu.typed


def test_action_pipeline_order(cfg, fake_cu, monkeypatch):
    snapshot(fake_cu, '[@e1] button "Seven"')
    order = []
    for module, name in ((safety, "is_dangerous"), (agent_cu, "snapshot_interactive"),
                         (agent_cu, "parse_elements"), (matching, "resolve"),
                         (agent_cu, "click"), (safety, "log_action")):
        original = getattr(module, name)
        def traced(*args, _name=name, _original=original, **kwargs):
            order.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, traced)
    assert core.click("Editor", "Seven", cfg)["ok"]
    assert order == ["is_dangerous", "snapshot_interactive", "parse_elements", "resolve",
                     "is_dangerous", "click", "log_action"]


@pytest.mark.parametrize("contents", [
    '[@e1] button "Body"\n[@e2] button "Body"',
    '[@e1] button "Body"\n[@e2] link "Body"',
    '[@e1] button "Body text"\n[@e2] button "Body content"',
])
def test_type_ambiguous_noneditable_targets(cfg, fake_cu, contents):
    snapshot(fake_cu, contents)
    result = core.type_text("Editor", "Body", "SENTINEL-7f3a", cfg)
    assert result["error"]["code"] == "TARGET_NOT_EDITABLE"
    assert len(result["candidates"]) == 2
    assert not fake_cu.clicked and not fake_cu.typed
    assert records(cfg)[0]["result"] == "error"
    assert records(cfg)[0]["error_code"] == "TARGET_NOT_EDITABLE"
    assert_private(result, cfg, "SENTINEL-7f3a")


@pytest.mark.parametrize("app", ["", " ", "Editor&Calculator"])
@pytest.mark.parametrize("action", ["click", "type"])
def test_invalid_app_has_app_error(cfg, fake_cu, app, action):
    result = (core.click(app, "Body", cfg) if action == "click" else
              core.type_text(app, "Body", "SENTINEL-7f3a", cfg))
    assert result["error"]["code"] == "APP_NOT_FOUND"
    assert result["error"]["suggestion"]
    assert not fake_cu.calls and not fake_cu.clicked and not fake_cu.typed
    assert records(cfg)[0]["error_code"] == "APP_NOT_FOUND"
    if action == "type":
        assert_private(result, cfg, "SENTINEL-7f3a")


@pytest.mark.parametrize("ok", [True, False])
def test_action_result_trimming_is_bounded_even_without_progress(monkeypatch, ok):
    original = json.dumps
    calls = []
    def counted(*args, **kwargs):
        calls.append(None)
        return original(*args, **kwargs)
    monkeypatch.setattr(core.json, "dumps", counted)
    payload = {"ok": ok, "action": "type", "unexpected": "x" * 20000}
    if not ok:
        payload["error"] = {"code": "APP_NOT_FOUND"}
    result = core._action_result(payload, None)
    assert len(calls) <= 16
    assert result["ok"] is ok
    assert len(original(result, indent=2)) < 1500
    if not ok:
        assert result["error"]["code"] == "APP_NOT_FOUND"


def test_server_tools_pass_one_shared_ollama_client(cfg, monkeypatch):
    shared = make_ollama(cfg)
    constructed = []
    def client(config):
        assert config is cfg
        constructed.append(shared)
        return shared
    monkeypatch.setattr(server, "OllamaClient", client)
    seen = []
    for name in ("look_at_screen", "click", "type_text", "ping", "release_model", "health"):
        def capture(*args, _name=name, ollama=None, **kwargs):
            seen.append((_name, ollama))
            return {"ok": True}
        monkeypatch.setattr(core, name, capture)
    mcp = server.build_server(cfg)
    for name, args in (
        ("look_at_screen", {"question": "Status?"}),
        ("click", {"app": "Editor", "target": "Body"}),
        ("type_text", {"app": "Editor", "target": "Body", "text": "private"}),
        ("health", {}), ("ping", {}), ("release_model", {}),
    ):
        asyncio.run(mcp.call_tool(name, args))
    assert len(constructed) == 1 and len(seen) == 6
    assert all(ollama is shared for _, ollama in seen)


@pytest.mark.parametrize("action", ["click", "type_text"])
def test_action_model_pick_uses_shared_client_and_memory_policy(cfg, fake_cu, monkeypatch, action):
    snapshot(fake_cu, '[@e1] textfield "Body"\n[@e2] textfield "Body"')
    from mcp_computer_use import memory
    policy = []
    def keep_alive(config, session=False, keep_warm=False):
        assert config is cfg
        policy.append((session, keep_warm))
        return "7m", "test_policy"
    monkeypatch.setattr(memory, "keep_alive_for", keep_alive)
    def no_autostart(*args, **kwargs):
        raise AssertionError("actions must not autostart Ollama")
    monkeypatch.setattr(autostart, "ensure_ollama", no_autostart)
    sent = []
    shared = make_ollama(cfg, chat={"ref": "@e2"}, record=sent)
    monkeypatch.setattr(server, "OllamaClient", lambda config: shared)
    args = {"app": "Editor", "target": "Body", "session_id": "audit_only"}
    if action == "type_text":
        args["text"] = "SENTINEL-7f3a"
    asyncio.run(server.build_server(cfg).call_tool(action, args))
    assert policy == [(False, False)]
    assert len(sent) == 1 and sent[0]["keep_alive"] == "7m"
    assert records(cfg)[0]["result"] == "done"
    if action == "click":
        assert fake_cu.clicked == {"Editor": ["@e2"]}
    else:
        assert fake_cu.typed == {"Editor": [("@e2", "SENTINEL-7f3a")]}
    assert not list(sessions_dir(cfg).glob("*__*.json"))


def test_server_and_memory_watcher_share_client(cfg, monkeypatch):
    shared = make_ollama(cfg)
    cfg.memory_watch = True
    cfg.health_http = False
    monkeypatch.setattr(server, "load_config", lambda path: cfg)
    constructed = []
    def client(config):
        constructed.append(shared)
        return shared
    monkeypatch.setattr(server, "OllamaClient", client)
    seen = []
    class Watcher:
        def __init__(self, config, ollama):
            assert config is cfg
            seen.append(("watcher", ollama))
        def start(self):
            seen.append(("watcher_started", shared))
    class MCP:
        def run(self, transport):
            assert transport == "stdio"
    def build(config, ollama=None):
        assert config is cfg
        seen.append(("server", ollama))
        return MCP()
    monkeypatch.setattr(server, "MemoryWatcher", Watcher)
    monkeypatch.setattr(server, "build_server", build)
    server.main([])
    assert constructed == [shared]
    assert seen == [("watcher", shared), ("watcher_started", shared), ("server", shared)]
