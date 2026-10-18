"""Mocked CLI action contracts and typed-text confidentiality."""
import json
import subprocess
from pathlib import Path

import pytest

from mcp_computer_use import agent_cu
from mcp_computer_use.errors import (
    AGENT_CU_FAILED, AGENT_CU_NOT_INSTALLED, APP_NOT_FOUND, INVALID_TARGET,
    CuError, error_payload,
)


def test_snapshot_exact_argv(fake_cu):
    fake_cu.snaps["Editor"] = '[@e1] document "Body"'
    assert agent_cu.snapshot_interactive("Editor", cmd="custom", timeout=1.25) == fake_cu.snaps["Editor"]
    assert fake_cu.calls == [["snapshot", "-a", "Editor", "-i", "-c", "--human"]]


def test_click_exact_argv_and_record(fake_cu):
    assert agent_cu.click("@e12", "Calculator") == "clicked"
    assert fake_cu.calls == [["click", "@e12", "-a", "Calculator"]]
    assert fake_cu.clicked == {"Calculator": ["@e12"]}


@pytest.mark.parametrize("text", ["hello", "--submit", "-leading", "two\r\nlines", "שלום"])
def test_type_exact_argv_and_record(fake_cu, text):
    assert agent_cu.type_text(text, "Editor", "@e4") == "typed"
    assert fake_cu.calls == [["type", "-s", "@e4", "-a", "Editor", "--", text]]
    assert fake_cu.typed == {"Editor": [("@e4", text)]}
    assert not fake_cu.clicked


@pytest.mark.parametrize("ref", ["", "e1", "@e", "@e-1", "@e1\n", " @e1", "@e1 extra", "@e1;cmd", None, 1])
@pytest.mark.parametrize("action", ["click", "type"])
def test_bad_ref_never_runs(fake_cu, ref, action):
    with pytest.raises(CuError) as caught:
        if action == "click":
            agent_cu.click(ref, "Editor")
        else:
            agent_cu.type_text("private", "Editor", ref)
    assert caught.value.code == INVALID_TARGET
    assert not fake_cu.calls


@pytest.mark.parametrize("stderr, code, kind", [
    (json.dumps({"type": "application_not_found", "message": "missing"}), APP_NOT_FOUND, "application_not_found"),
    (json.dumps({"type": "element_not_found", "message": "application not found"}), AGENT_CU_FAILED, "element_not_found"),
    (json.dumps({"type": "permission_denied", "message": "no window"}), AGENT_CU_FAILED, "permission_denied"),
    (json.dumps({"message": "app not found"}), APP_NOT_FOUND, "app not found"),
    (json.dumps({"type": None, "message": "app not found"}), APP_NOT_FOUND, "app not found"),
    (json.dumps({"type": 12, "message": "app not found"}), APP_NOT_FOUND, "app not found"),
    (json.dumps({"type": [], "message": "permission denied"}), AGENT_CU_FAILED, "permission denied"),
    (json.dumps("not found"), APP_NOT_FOUND, "not found"),
    ("App not found: Missing", APP_NOT_FOUND, "not found"),
    ("no such app", APP_NOT_FOUND, "no such app"),
    ("no window", APP_NOT_FOUND, "no window"),
    ("boom", AGENT_CU_FAILED, "boom"),
])
def test_run_stderr_mapping(fake_cu, monkeypatch, stderr, code, kind):
    monkeypatch.setattr(agent_cu.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", stderr))
    with pytest.raises(CuError) as caught:
        agent_cu.click("@e1", "Editor")
    assert caught.value.code == code
    assert kind in caught.value.message


def test_run_parses_json_before_diagnostic_truncation(fake_cu, monkeypatch):
    stderr = json.dumps({"message": "element not found " + "x" * 500, "type": "element_not_found"})
    monkeypatch.setattr(agent_cu.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", stderr))
    with pytest.raises(CuError) as caught:
        agent_cu.run(["snapshot", "-a", "Editor"])
    assert caught.value.code == AGENT_CU_FAILED
    assert "element_not_found" in caught.value.message


def test_parse_inner_quotes_attributes_and_ref_only():
    snapshot = '''window "Settings"
  group "Panel"
    [@e1] button "Say "hello" now" val="checked" id=save_button
    button "Not actionable"
    [@e2] textfield "" id=input val="some value"
    [@e3] separator
  [@e4] button "Sibling"
[@e5] button "Root"
[not-a-ref] button "Ignored"
'''
    assert agent_cu.parse_elements(snapshot) == [
        agent_cu.Element("@e1", "button", 'Say "hello" now', "checked", "save_button", "Panel"),
        agent_cu.Element("@e2", "textfield", "", "some value", "input", "Panel"),
        agent_cu.Element("@e3", "separator", "", context="Panel"),
        agent_cu.Element("@e4", "button", "Sibling", context="Settings"),
        agent_cu.Element("@e5", "button", "Root"),
    ]


def test_parse_nearest_named_ancestor_and_dedent():
    snapshot = '''window "Application"
  group "First"
    group
      [@e1] button "Close"
  group "Second"
    [@e2] button "Close"
  group
    [@e3] button "Third"
'''
    assert [e.context for e in agent_cu.parse_elements(snapshot)] == ["First", "Second", "Application"]


def test_parse_context_bounded_and_unicode():
    parent = "א" * 60
    elements = agent_cu.parse_elements(f'window "{parent}"\n\t[@e10] searchfield "חיפוש" val="" id=search')
    assert elements == [agent_cu.Element("@e10", "searchfield", "חיפוש", "", "search", parent[:40])]


@pytest.mark.parametrize("snapshot", ["", "\n\n", "not a snapshot", 'window "Title"\n  button "Save"'])
def test_parse_no_actionable_elements(snapshot):
    assert agent_cu.parse_elements(snapshot) == []


SENTINELS = [
    "SENTINEL-7f3a",
    "line1\r\nSENTINEL-B",
    "סודי-SENTINEL",
    'private-"quoted"-SENTINEL\r\nvalue',
]
FAILURES = ["exit", "json", "escaped_truncated", "bad_kind", "long_kind", "non_string_kind", "stdout", "timeout", "os_error", "value_error"]


@pytest.mark.parametrize("sentinel", SENTINELS)
@pytest.mark.parametrize("failure", FAILURES)
def test_type_failures_never_leak_text(fake_cu, monkeypatch, sentinel, failure):
    escaped = json.dumps(sentinel, ensure_ascii=True)[1:-1]

    def failing(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=sentinel, stderr=escaped)
        if failure == "os_error":
            raise OSError(sentinel)
        if failure == "value_error":
            raise ValueError(sentinel)
        stderr = sentinel
        if failure == "json":
            stderr = json.dumps({"type": "element_not_found", "message": sentinel})
        elif failure == "escaped_truncated":
            stderr = json.dumps({"type": "element_not_found", "message": sentinel})[:-4]
        elif failure == "bad_kind":
            stderr = json.dumps({"type": sentinel, "message": sentinel})
        elif failure == "long_kind":
            stderr = json.dumps({"type": "x" * 41, "message": sentinel})
        elif failure == "non_string_kind":
            stderr = json.dumps({"type": [sentinel], "message": sentinel})
        elif failure == "stdout":
            stderr = ""
        return subprocess.CompletedProcess(args, 7, sentinel, stderr)

    monkeypatch.setattr(agent_cu.subprocess, "run", failing)
    with pytest.raises(CuError) as caught:
        agent_cu.type_text(sentinel, "Editor", "@e4")
    error = caught.value
    assert error.code == AGENT_CU_FAILED
    expected_kind = {"timeout": "timeout", "os_error": "os_error", "value_error": "value_error", "json": "element_not_found"}.get(failure, "exit_7")
    assert error.message == f"agent-cu type {expected_kind}: app='Editor' ref=@e4"
    assert not error.extra
    payload = error_payload(error.code, error.message, **error.extra)
    for rendered in [str(error), repr(error), json.dumps(payload, ensure_ascii=True), json.dumps(payload, ensure_ascii=False)]:
        for text in [sentinel, escaped]:
            assert all(text[i:i + 6] not in rendered for i in range(len(text) - 5))
    assert error.__suppress_context__
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("kind, code", [("application_not_found", APP_NOT_FOUND), ("element_not_found", AGENT_CU_FAILED)])
def test_type_json_mapping(fake_cu, monkeypatch, kind, code):
    monkeypatch.setattr(agent_cu.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 1, "SENTINEL-7f3a", json.dumps({"type": kind, "message": "SENTINEL-7f3a not found"})))
    with pytest.raises(CuError) as caught:
        agent_cu.type_text("SENTINEL-7f3a", "Editor", "@e1")
    assert caught.value.code == code
    assert caught.value.message == f"agent-cu type {kind}: app='Editor' ref=@e1"


def test_type_missing_executable_safe(fake_cu, monkeypatch):
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: None)
    with pytest.raises(CuError) as caught:
        agent_cu.type_text("SENTINEL-7f3a", "Editor", "@e1", cmd="SENTINEL-7f3a")
    assert caught.value.code == AGENT_CU_NOT_INSTALLED
    assert caught.value.message == "agent-cu type not_installed: app='Editor' ref=@e1"
    assert not fake_cu.calls


def test_type_resolve_os_error_safe(fake_cu, monkeypatch):
    def fail(cmd):
        raise OSError("SENTINEL-7f3a")
    monkeypatch.setattr(agent_cu, "resolve_cmd", fail)
    with pytest.raises(CuError) as caught:
        agent_cu.type_text("SENTINEL-7f3a", "Editor", "@e1")
    assert caught.value.message == "agent-cu type os_error: app='Editor' ref=@e1"


def test_type_error_message_bounded(fake_cu):
    fake_cu.fail = "timeout"
    with pytest.raises(CuError) as caught:
        agent_cu.type_text("SENTINEL-7f3a", "a" * 10000, "@e" + "1" * 10000)
    assert len(caught.value.message) < 220


@pytest.mark.parametrize("code", [
    "DANGEROUS_ACTION_NEEDS_CONFIRMATION", "TARGET_NOT_FOUND", "TARGET_AMBIGUOUS",
    "INVALID_TARGET", "TEXT_TOO_LONG", "TARGET_NOT_EDITABLE",
])
def test_new_error_metadata(code):
    payload = error_payload(code, "message")
    assert payload["error"]["retryable"] is True
    assert payload["error"]["suggestion"]


def test_cli_options_and_timeout_forwarded(fake_cu, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "ok", "")
    resolved = []
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: resolved.append(cmd) or "/fake/cu")
    monkeypatch.setattr(agent_cu.subprocess, "run", run)
    agent_cu.snapshot_interactive("Editor", "custom-cu", 1.25)
    agent_cu.click("@e1", "Editor", "custom-cu", 2.5)
    agent_cu.type_text("--submit", "Editor", "@e2", "custom-cu", 3.75)
    assert resolved == ["custom-cu"] * 3
    assert [args for args, _ in calls] == [
        ["/fake/cu", "snapshot", "-a", "Editor", "-i", "-c", "--human"],
        ["/fake/cu", "click", "@e1", "-a", "Editor"],
        ["/fake/cu", "type", "-s", "@e2", "-a", "Editor", "--", "--submit"],
    ]
    assert [kw["timeout"] for _, kw in calls] == [1.25, 2.5, 3.75]
    for _, kw in calls:
        assert kw == dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=kw["timeout"])


@pytest.fixture
def npm_shim(tmp_path):
    directory = tmp_path / "npm with spaces"
    directory.mkdir()
    entry = directory / "node_modules" / "agent-cu" / "bin" / "agent-cu.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fake entry; never executed", encoding="utf-8")
    shim = directory / "agent-cu.cmd"
    shim.write_text(
        '@ECHO off\nSET dp0=%~dp0\n'
        'IF EXIST "%dp0%\\node.exe" (SET "_prog=%dp0%\\node.exe") ELSE (SET "_prog=node")\n'
        '"%_prog%" "%dp0%\\node_modules\\agent-cu\\bin\\agent-cu.js" %*\n',
        encoding="utf-8",
    )
    node = directory / "node.exe"
    node.write_bytes(b"fake node; never executed")
    return shim, node, entry


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".CMD", ".BAT"])
@pytest.mark.parametrize("text", ['"&calc&"', "%USERNAME%", "a\r\nb"])
def test_type_npm_shim_runs_node_directly(fake_cu, monkeypatch, npm_shim, suffix, text):
    shim, node, entry = npm_shim
    renamed = shim.with_suffix(suffix)
    if renamed != shim:
        shim.rename(renamed)
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: str(renamed))
    calls = []
    def capture(args, **kwargs):
        calls.append(args)
        assert Path(args[0]).suffix.lower() == ".exe"
        assert not kwargs.get("shell", False)
        return subprocess.CompletedProcess(args, 0, "typed", "")
    monkeypatch.setattr(agent_cu.subprocess, "run", capture)
    assert agent_cu.type_text(text, "Editor", "@e4") == "typed"
    assert calls == [[str(node), str(entry.resolve()), "type", "-s", "@e4", "-a", "Editor", "--", text]]


def test_npm_shim_uses_node_exe_from_path(fake_cu, monkeypatch, npm_shim, tmp_path):
    shim, node, entry = npm_shim
    node.unlink()
    path_node = tmp_path / "node.exe"
    path_node.write_bytes(b"fake node")
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: str(shim))
    monkeypatch.setattr(agent_cu.shutil, "which", lambda cmd: str(path_node) if cmd == "node.exe" else None)
    calls = []
    monkeypatch.setattr(agent_cu.subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    agent_cu.type_text("%USERNAME%", "Editor", "@e1")
    assert calls[0][:2] == [str(path_node), str(entry.resolve())]


@pytest.mark.parametrize("failure", ["missing_shim", "unknown_shim", "missing_entry", "missing_node", "batch_node", "outside_modules"])
def test_type_unresolved_shim_refused(fake_cu, monkeypatch, npm_shim, failure):
    shim, node, entry = npm_shim
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: str(shim))
    monkeypatch.setattr(agent_cu.shutil, "which", lambda cmd: None)
    if failure == "missing_shim":
        shim.unlink()
    elif failure == "unknown_shim":
        shim.write_text("@echo off\nunknown %*", encoding="utf-8")
    elif failure == "missing_entry":
        entry.unlink()
    elif failure == "missing_node":
        node.unlink()
    elif failure == "batch_node":
        node.unlink()
        monkeypatch.setattr(agent_cu.shutil, "which", lambda cmd: str(shim))
    elif failure == "outside_modules":
        shim.write_text('"node" "%dp0%\\node_modules\\..\\agent-cu.js" %*', encoding="utf-8")
        (shim.parent / "agent-cu.js").write_text("// not a package entry", encoding="utf-8")
    with pytest.raises(CuError) as caught:
        agent_cu.type_text('"&calc&"', "Editor", "@e4")
    assert caught.value.code == AGENT_CU_FAILED
    assert caught.value.message == "agent-cu type unsafe_shell_shim: app='Editor' ref=@e4"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not fake_cu.calls


@pytest.mark.parametrize("action", ["click", "snapshot", "text"])
def test_non_type_actions_resolve_npm_shim(fake_cu, monkeypatch, npm_shim, action):
    shim, node, entry = npm_shim
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: str(shim))
    calls = []
    monkeypatch.setattr(agent_cu.subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "ok", ""))
    if action == "click":
        agent_cu.click("@e1", "Editor")
    elif action == "snapshot":
        agent_cu.snapshot_interactive("Editor")
    else:
        agent_cu.window_text("Editor")
    assert calls[0][:3] == [str(node), str(entry.resolve()), action]


@pytest.mark.parametrize("char", list('&|<>^%"\r\n'))
@pytest.mark.parametrize("action", ["click", "snapshot", "window_snapshot", "text", "type"])
def test_reject_app_shell_metacharacters(fake_cu, char, action):
    app = f"Editor{char}bad"
    with pytest.raises(CuError) as caught:
        if action == "click":
            agent_cu.click("@e1", app)
        elif action == "snapshot":
            agent_cu.snapshot_interactive(app)
        elif action == "window_snapshot":
            agent_cu.window_snapshot(app)
        elif action == "text":
            agent_cu.window_text(app)
        else:
            agent_cu.type_text("SENTINEL-7f3a", app, "@e1")
    assert caught.value.code == INVALID_TARGET
    assert not fake_cu.calls


def test_type_nul_value_error_is_bounded(fake_cu, monkeypatch):
    def fail(args, **kwargs):
        assert args[-1] == "SENTINEL-7f3a\x00"
        raise ValueError("embedded null byte: SENTINEL-7f3a")
    monkeypatch.setattr(agent_cu.subprocess, "run", fail)
    with pytest.raises(CuError) as caught:
        agent_cu.type_text("SENTINEL-7f3a\x00", "Editor", "@e1")
    assert caught.value.code == AGENT_CU_FAILED
    assert caught.value.message == "agent-cu type value_error: app='Editor' ref=@e1"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_parse_calculator_he_fixture():
    snapshot = (Path(__file__).parent / "fixtures" / "snapshots" / "calculator_he.txt").read_text(encoding="utf-8")
    elements = agent_cu.parse_elements(snapshot)
    assert len(elements) == 37
    by_id = {element.id: element for element in elements}
    assert by_id["num7Button"] == agent_cu.Element("@e32", "button", "שבע", id="num7Button", context="מקלדת מספרית")
    assert by_id["plusButton"] == agent_cu.Element("@e23", "button", "ועוד", id="plusButton", context="אופרטורים רגילים")
    assert by_id["num8Button"] == agent_cu.Element("@e33", "button", "שמונה", id="num8Button", context="מקלדת מספרית")
    assert by_id["equalButton"] == agent_cu.Element("@e24", "button", "שווה", id="equalButton", context="אופרטורים רגילים")
    assert by_id["HistoryButton"].context == "מחשבון"
    assert by_id["ClearMemoryButton"].context == "פקדי זיכרון"
    assert by_id["NormalAlwaysOnTopButton"].context == "מחשבון"
    # The fixture's expression is a non-ref node, so test its label without
    # changing the ref-only element contract or the fixture itself.
    expression_line = next(line for line in snapshot.splitlines() if "id=CalculatorExpression" in line)
    assert not any(e.id == "CalculatorExpression" for e in elements)
    expression = agent_cu.parse_elements(expression_line.replace("statictext", "[@e99] statictext", 1))
    assert expression == [agent_cu.Element("@e99", "statictext", "הביטוי הוא ", id="CalculatorExpression")]


def test_editable_suggestion_period():
    assert error_payload("TARGET_NOT_EDITABLE", "message")["error"]["suggestion"] == (
        "Target a text field; use look_at_screen to find its label."
    )
