"""Auto-start of the local Ollama (mocked subprocess / network)."""
import json

import httpx
import pytest

from mcp_computer_use import autostart, core
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.ollama_client import OllamaClient
from mcp_computer_use.sessions import sessions_dir


class World:
    """Fake local Ollama that is DOWN until spawn() is called (optionally after N polls)."""

    def __init__(self, cfg, up=False, up_after_polls=0, fail_chat_times=0):
        self.up, self.up_after, self.polls = up, up_after_polls, 0
        self.spawned = []
        self.fail_chat_times = fail_chat_times
        self.chat_calls = 0
        self.client = OllamaClient(cfg, transport=httpx.MockTransport(self.handle))

    def spawn(self, exe, host, port):
        self.spawned.append((exe, host, port))
        self.starting = True

    def handle(self, req):
        p = req.url.path
        if not self.up:
            if getattr(self, "starting", False):
                self.polls += 1
                if self.polls > self.up_after:
                    self.up = True
            if not self.up:
                raise httpx.ConnectError("refused", request=req)
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if p == "/api/chat":
            self.chat_calls += 1
            if self.fail_chat_times > 0:
                self.fail_chat_times -= 1
                self.up = False
                self.starting = False
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"message": {"content": json.dumps(
                {"status": "finished", "summary": "ok", "confidence": 0.9})},
                "eval_count": 5, "eval_duration": 10**9, "total_duration": 10**9})
        return httpx.Response(404)


@pytest.fixture
def local_cfg(tmp_path):
    return Config(ollama_url="http://127.0.0.1:11435", model="gpt-oss:20b",
                  sessions_dir=str(tmp_path / ".sessions"), autostart_timeout=3)


@pytest.fixture
def env(monkeypatch, local_cfg):
    def make(**kw):
        w = World(local_cfg, **kw)
        monkeypatch.setattr(autostart, "_spawn", w.spawn)
        monkeypatch.setattr(autostart, "find_ollama_exe", lambda: r"C:\Ollama\ollama.exe")
        monkeypatch.setattr(autostart, "port_open", lambda h, p, timeout=0.5: w.up or bool(w.spawned))
        monkeypatch.setattr(autostart.time, "sleep", lambda s: None)
        return w
    return make


def test_target_only_local():
    assert autostart.target(Config(ollama_url="http://127.0.0.1:11435")) == ("127.0.0.1", 11435)
    assert autostart.target(Config(ollama_url="http://localhost")) == ("127.0.0.1", 11434)
    assert autostart.target(Config(ollama_url="http://Zvis-MacBook-Pro.local:11434")) is None


def test_starts_when_down_and_waits_for_tags(local_cfg, env):
    w = env(up_after_polls=2)
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["started"] and r["reachable"] and r["action"] == "started"
    assert w.spawned == [(r"C:\Ollama\ollama.exe", "127.0.0.1", 11435)]
    assert autostart.state["autostarted"] is True
    assert not (sessions_dir(local_cfg) / autostart.LOCK_NAME).exists()  # lock released


def test_no_start_when_running(local_cfg, env):
    w = env(up=True)
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "already_running"
    assert w.spawned == []


def test_disabled_and_remote_never_start(local_cfg, env, monkeypatch):
    w = env()
    local_cfg.autostart_ollama = False
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "disabled"
    local_cfg.autostart_ollama = True
    local_cfg.ollama_url = "http://Zvis-MacBook-Pro.local:11434"
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "not_local"
    assert w.spawned == []


def test_lock_prevents_double_start(local_cfg, env):
    w = env()
    lock = sessions_dir(local_cfg) / autostart.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text("999")  # another server process is starting it
    local_cfg.autostart_timeout = 0.2
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["action"] == "waiting_for_other_start" and w.spawned == [] and not r["reachable"]


def test_stale_lock_is_ignored(local_cfg, env):
    import os, time
    w = env()
    lock = sessions_dir(local_cfg) / autostart.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text("999")
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert autostart.ensure_ollama(local_cfg, w.client)["started"] is True


def test_port_busy_does_not_spawn(local_cfg, env, monkeypatch):
    w = env()
    monkeypatch.setattr(autostart, "port_open", lambda h, p, timeout=0.5: True)
    local_cfg.autostart_timeout = 0.2
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["action"] == "port_busy_waiting" and w.spawned == []


def test_missing_exe(local_cfg, env, monkeypatch):
    w = env()
    monkeypatch.setattr(autostart, "find_ollama_exe", lambda: None)
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "ollama_not_found"


def test_find_exe_localappdata(tmp_path, monkeypatch):
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(autostart.shutil, "which", lambda n: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert autostart.find_ollama_exe() == str(exe)


def test_spawn_sets_ollama_host_only_for_child(monkeypatch):
    import os
    real = autostart._spawn_impl
    seen = {}
    class P:
        def __init__(self, args, **kw):
            seen["args"], seen["env"], seen["kw"] = args, kw["env"], kw
    monkeypatch.setattr(autostart.subprocess, "Popen", P)
    monkeypatch.setenv("OLLAMA_HOST", "Zvis-MacBook-Pro.local:11434")
    real("ollama", "127.0.0.1", 11435)
    assert seen["args"] == ["ollama", "serve"] and seen["env"]["OLLAMA_HOST"] == "127.0.0.1:11435"
    assert os.environ["OLLAMA_HOST"] == "Zvis-MacBook-Pro.local:11434"  # parent untouched
    assert seen["kw"]["stdout"] is autostart.subprocess.DEVNULL


def test_look_at_screen_autostarts_on_stateless_call(local_cfg, env, fake_cu):
    w = env(up_after_polls=1)
    res = core.look_at_screen("Is Claude finished?", "Claude", local_cfg, w.client)
    assert "error" not in res and res["summary"] == "ok"
    assert res["memory"]["ollama_autostarted"] is True and len(w.spawned) == 1


def test_check_only_on_first_call_of_session(local_cfg, env, fake_cu, monkeypatch):
    w = env(up=True)
    calls = []
    orig = autostart.ensure_ollama
    monkeypatch.setattr(autostart, "ensure_ollama", lambda c, o, **k: calls.append(1) or orig(c, o, **k))
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    core.look_at_screen("q3", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert len(calls) == 1  # only the first call of the session
    core.look_at_screen("q4", "Claude", local_cfg, w.client)
    core.look_at_screen("q5", "Claude", local_cfg, w.client)
    assert len(calls) == 3  # every call without session_id


def test_mid_session_unreachable_restarts_once_and_retries(local_cfg, env, fake_cu):
    w = env(up=True)
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    w.fail_chat_times = 1  # Ollama dies before the next chat
    res = core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert "error" not in res and res["summary"] == "ok"
    assert len(w.spawned) == 1 and w.chat_calls == 3


def test_mid_session_gives_up_after_one_retry(local_cfg, env, fake_cu):
    w = env(up=True)
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    w.fail_chat_times = 5
    res = core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert res["error"]["code"] == "OLLAMA_UNREACHABLE" and len(w.spawned) == 1 and w.chat_calls == 3


def test_health_reports_autostart(local_cfg, env):
    w = env(up_after_polls=0)
    autostart.ensure_ollama(local_cfg, w.client)
    h = core.health(local_cfg, w.client)
    assert h["ollama_autostarted"] is True and h["autostart_enabled"] is True


def test_config_env_autostart():
    c = load_config(env={"MCP_CU_AUTOSTART_OLLAMA": "0", "MCP_CU_AUTOSTART_TIMEOUT": "20"})
    assert c.autostart_ollama is False and c.autostart_timeout == 20.0
    assert Config().autostart_ollama is True and Config().autostart_timeout == 10.0


# ---------------------------------------------------------------- ping
class PingOllama:
    def __init__(self, cfg, reply="pong", loaded=False, down=False, status=200):
        self.reply, self.loaded, self.down, self.status = reply, loaded, down, status
        self.bodies = []
        self.client = OllamaClient(cfg, transport=httpx.MockTransport(self.handle))

    def handle(self, req):
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        p = req.url.path
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b", "size": 1}] if self.loaded else []})
        if p == "/api/chat":
            self.bodies.append(json.loads(req.content))
            if self.status != 200:
                return httpx.Response(self.status, json={})
            return httpx.Response(200, json={"message": {"content": self.reply}})
        return httpx.Response(404)


def test_ping_ok_cold(cfg, fake_cu):
    o = PingOllama(cfg, reply="pong")
    r = core.ping(cfg, o.client)
    assert r["ok"] and r["server"] and r["ollama"] and r["model_reply_ok"] and r["cold"] is True
    assert set(r) >= {"ok", "server", "ollama", "model_reply_ok", "model", "seconds", "cold"} and "error" not in r
    b = o.bodies[0]
    assert b["keep_alive"] == "3m" and b["options"]["num_predict"] <= 128 and "pong" in b["messages"][0]["content"]
    assert fake_cu.calls == []  # no screen reading


def test_ping_warm_and_bad_reply(cfg):
    r = core.ping(cfg, PingOllama(cfg, reply="Pong.", loaded=True).client)
    assert r["ok"] and r["cold"] is False
    r = core.ping(cfg, PingOllama(cfg, reply="hello", loaded=True).client)
    assert not r["ok"] and r["ollama"] and not r["model_reply_ok"] and "unexpected" in r["error"]


def test_ping_ollama_down_not_local(cfg):
    r = core.ping(cfg, PingOllama(cfg, down=True).client)  # cfg URL is not localhost: no autostart
    assert r == {**r, "ok": False, "server": True, "ollama": False, "model_reply_ok": False}
    assert "not_local" in r["error"]


def test_ping_autostarts(local_cfg, env):
    w = env(up_after_polls=1)
    r = core.ping(local_cfg, w.client)
    assert r["ollama"] and r["ollama_autostarted"] and len(w.spawned) == 1


def test_ping_model_missing(cfg):
    r = core.ping(cfg, PingOllama(cfg, status=404).client)
    assert not r["ok"] and r["error"].startswith("MODEL_NOT_FOUND")
