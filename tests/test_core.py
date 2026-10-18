import json

import httpx

from mcp_computer_use import core, agent_cu
from mcp_computer_use.agent_cu import AppInfo, choose_app, parse_apps

from conftest import make_ollama

EXPECTED_KEYS = {"windows_seen", "status", "summary", "confidence", "model_used", "duration_seconds", "memory_mb"}


def test_success_shape_and_request(cfg, fake_cu):
    sent = []
    res = core.look_at_screen("Is Claude finished?", None, cfg, make_ollama(cfg, record=sent))
    assert EXPECTED_KEYS <= res.keys() and "error" not in res
    assert res["windows_seen"] == ["Claude"] and res["status"] == "finished"
    assert res["memory_mb"] == 11 * 1024 and res["model_used"] == "gpt-oss:20b"
    assert res["tokens_per_second"] == 12.0
    body = sent[0]
    assert body["think"] == "low" and body["format"]["type"] == "object"
    assert body["options"] == {"num_ctx": 4096, "temperature": 0.0}
    user = body["messages"][1]["content"]
    assert "916 UI tests" in user and "How is Claude doing" not in user  # only the short text is sent


def test_non_gpt_oss_disables_thinking(cfg, fake_cu):
    cfg.model = "qwen3:4b"
    sent = []
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, record=sent))
    assert sent[0]["think"] is False


def test_model_cannot_contradict_detected_status(cfg, fake_cu):
    o = make_ollama(cfg, chat={"status": "working", "summary": "x", "confidence": 3})
    res = core.look_at_screen("q", "Claude", cfg, o)
    assert res["status"] == "finished" and res["confidence"] == 1.0


def test_ollama_unreachable_fallback(cfg, fake_cu):
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, unreachable=True))
    err = res["error"]
    assert err["code"] == "OLLAMA_UNREACHABLE" and err["retryable"] and err["suggestion"]
    assert res["extracted"]["status"] == "finished"
    assert "be8b48e" in res["extracted"]["last_message"]


def test_model_timeout_fallback(cfg, fake_cu):
    def slow(req, body):
        raise httpx.ReadTimeout("slow", request=req)
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, chat=slow, loaded=False))
    assert res["error"]["code"] == "MODEL_TIMEOUT"
    assert res["timeout_seconds"] >= cfg.cold_load_extra  # cold-load allowance applied
    assert res["extracted"]["last_message"]


def test_model_not_found_and_bad_json(cfg, fake_cu):
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, chat=lambda r, b: httpx.Response(404, json={})))
    assert res["error"]["code"] == "MODEL_NOT_FOUND" and not res["error"]["retryable"]
    bad = lambda r, b: httpx.Response(200, json={"message": {"content": "Let me think..."}})
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, chat=bad))
    assert res["error"]["code"] == "MODEL_FAILED"


def test_raw_fallback_truncated(cfg, fake_cu):
    fake_cu.texts["Claude"] = "You said: q\nx\n" + "z" * 5000 + "\nNew session"
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, unreachable=True))
    assert len(res["extracted"]["last_message"]) <= core.RAW_FALLBACK_CHARS
    assert res["extracted"]["truncated"]


def test_app_not_found_lists_apps(cfg, fake_cu):
    res = core.look_at_screen("q", "Nope", cfg, make_ollama(cfg))
    assert res["error"]["code"] == "APP_NOT_FOUND"
    assert res["available_apps"] == ["Claude", "ChatGPT"]


def test_agent_cu_failures(cfg, fake_cu, monkeypatch):
    fake_cu.fail = "timeout"
    assert core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))["error"]["code"] == "AGENT_CU_FAILED"
    fake_cu.fail = "error"
    assert core.look_at_screen("q", None, cfg, make_ollama(cfg))["error"]["code"] == "AGENT_CU_FAILED"
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda c: None)
    assert core.look_at_screen("q", None, cfg, make_ollama(cfg))["error"]["code"] == "AGENT_CU_NOT_INSTALLED"


def test_no_apps(cfg, fake_cu):
    fake_cu.apps = []
    assert core.look_at_screen("q", None, cfg, make_ollama(cfg))["error"]["code"] == "APP_NOT_FOUND"


def test_choose_app():
    apps = [AppInfo("Claude"), AppInfo("ChatGPT", frontmost=True), AppInfo("Notepad")]
    assert choose_app(apps, "מה קלוד עושה?").name == "Claude"
    assert choose_app(apps, "what does codex say").name == "ChatGPT"
    assert choose_app(apps, "anything in notepad?").name == "Notepad"
    assert choose_app(apps, "what is on screen").name == "ChatGPT"  # frontmost
    assert choose_app([], "x") is None
    # the real foreground window beats agent-cu's frontmost flag; a named app still wins
    assert choose_app(apps, "what is on screen", foreground=apps[2]).name == "Notepad"
    assert choose_app(apps, "is claude done?", foreground=apps[2]).name == "Claude"


def test_parse_apps_keeps_pid():
    apps = parse_apps(json.dumps([{"name": "A", "pid": 7}, {"name": "B", "pid": True}, {"name": "C"}]))
    assert [a.pid for a in apps] == [7, None, None]


def test_foreground_app_by_pid_then_frontmost(monkeypatch):
    apps = [AppInfo("Claude", pid=1), AppInfo("ChatGPT", frontmost=True, pid=2), AppInfo("Notepad", pid=3)]
    assert agent_cu.foreground_app(apps, pid=3).name == "Notepad"
    assert agent_cu.foreground_app(apps, pid=99).name == "ChatGPT"  # unknown pid: frontmost flag
    monkeypatch.setattr(agent_cu, "foreground_pid", lambda: 1)
    assert agent_cu.foreground_app(apps).name == "Claude"
    monkeypatch.setattr(agent_cu, "foreground_pid", lambda: None)
    assert agent_cu.foreground_app([AppInfo("X")]) is None


def test_foreground_pid_is_safe():
    pid = agent_cu.foreground_pid()
    assert pid is None or (isinstance(pid, int) and pid > 0)


def test_look_without_app_reads_foreground_window(cfg, fake_cu, monkeypatch):
    fake_cu.apps = [{"name": "Claude", "pid": 1, "frontmost": True}, {"name": "ChatGPT", "pid": 2}]
    monkeypatch.setattr(agent_cu, "foreground_pid", lambda: 2)
    for app in (None, "", "  "):
        res = core.look_at_screen("What is on the screen?", app, cfg, make_ollama(cfg))
        assert "error" not in res and res["windows_seen"] == ["ChatGPT"]


def test_look_without_app_falls_back_to_foreground_when_guess_unreadable(cfg, fake_cu, monkeypatch):
    # the question names an app that has no readable window: read the foreground one instead
    fake_cu.apps = [{"name": "Ghost", "pid": 1}, {"name": "ChatGPT", "pid": 2}]
    monkeypatch.setattr(agent_cu, "foreground_pid", lambda: 2)
    res = core.look_at_screen("what does ghost show?", None, cfg, make_ollama(cfg))
    assert "error" not in res and res["windows_seen"] == ["ChatGPT"]


def test_look_explicit_app_unchanged(cfg, fake_cu, monkeypatch):
    monkeypatch.setattr(agent_cu, "foreground_pid", lambda: pytest_fail())
    res = core.look_at_screen("q", "Ghost", cfg, make_ollama(cfg))
    assert res["error"]["code"] == "APP_NOT_FOUND" and fake_cu.calls[0] == ["text", "-a", "Ghost"]


def pytest_fail():
    raise AssertionError("explicit app must not look at the foreground window")


def test_parse_apps_variants():
    assert [a.name for a in parse_apps('["A","B"]')] == ["A", "B"]
    apps = parse_apps(json.dumps({"apps": [{"app": "X", "focused": True}, {"title": "Y"}]}))
    assert apps[0].name == "X" and apps[0].frontmost and apps[1].name == "Y"
    assert [a.name for a in parse_apps("Foo\nBar\n")] == ["Foo", "Bar"]


def test_health(cfg, fake_cu):
    h = core.health(cfg, make_ollama(cfg))
    assert h["ok"] and h["model_present"] and h["model_loaded"] and h["agent_cu_available"]
    h = core.health(cfg, make_ollama(cfg, present=False, loaded=False))
    assert not h["ok"] and not h["model_loaded"] and h["problems"]
    h = core.health(cfg, make_ollama(cfg, unreachable=True))
    assert not h["ollama_reachable"] and not h["ok"]


def test_system_prompt_bundled(cfg):
    p = core.load_system_prompt(cfg)
    assert "JSON" in p and "Never invent windows" in p
