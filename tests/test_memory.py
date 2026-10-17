"""Smart model memory release: keep_alive policy, pressure unload, release_model, watcher."""
import json
import os
import time

import httpx

from mcp_computer_use import core, memory
from mcp_computer_use.config import load_config
from mcp_computer_use.ollama_client import OllamaClient
from mcp_computer_use.sessions import sessions_dir
from mcp_computer_use.memory import free_ram_mb as REAL_FREE_RAM


class FakeOllama:
    """Stateful mock: tracks loaded state, keep_alive values and unload calls."""

    def __init__(self, cfg, loaded=False, size=11 * 2**30):
        self.loaded, self.size = loaded, size
        self.keep_alives, self.unloads = [], 0
        self.client = OllamaClient(cfg, transport=httpx.MockTransport(self.handle))

    def handle(self, req):
        p = req.url.path
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b", "size": self.size,
                                                         "size_vram": self.size}] if self.loaded else []})
        if p == "/api/generate":
            body = json.loads(req.content)
            assert body["prompt"] == "" and body["keep_alive"] == 0
            self.unloads += 1
            self.loaded = False
            return httpx.Response(200, json={"done": True, "done_reason": "unload"})
        if p == "/api/chat":
            body = json.loads(req.content)
            self.keep_alives.append(body["keep_alive"])
            self.loaded = True
            return httpx.Response(200, json={"message": {"content": json.dumps(
                {"status": "finished", "summary": "done", "confidence": 0.9})},
                "eval_count": 10, "eval_duration": 10**9, "total_duration": 2 * 10**9})
        return httpx.Response(404)


def test_single_call_uses_idle_keep_alive(cfg, fake_cu):
    o = FakeOllama(cfg)
    res = core.look_at_screen("Is Claude finished?", "Claude", cfg, o.client)
    assert o.keep_alives == ["3m"]
    assert res["memory"]["keep_alive"] == "3m" and res["memory"]["reason"] == "single_call"
    assert res["memory"]["was_loaded"] is False and o.unloads == 0


def test_session_call_uses_session_keep_alive(cfg, fake_cu):
    o = FakeOllama(cfg)
    res = core.look_at_screen("q", "Claude", cfg, o.client, client="codex", session_id="s")
    assert o.keep_alives == ["10m"] and res["memory"]["reason"] == "session"


def test_recent_session_file_extends_stateless_call(cfg, fake_cu):
    o = FakeOllama(cfg)
    core.look_at_screen("q1", "Claude", cfg, o.client, client="codex", session_id="s")
    core.look_at_screen("q2", "Claude", cfg, o.client)  # other caller, no session
    assert o.keep_alives == ["10m", "10m"]


def test_old_session_file_does_not_count(cfg, fake_cu):
    d = sessions_dir(cfg)
    d.mkdir(parents=True)
    f = d / "codex__old.json"
    f.write_text("{}")
    past = time.time() - 11 * 60
    os.utime(f, (past, past))
    assert memory.active_session(cfg) is False
    assert memory.keep_alive_for(cfg) == ("3m", "single_call")
    os.utime(f, None)
    assert memory.keep_alive_for(cfg) == ("10m", "active_session")


def test_keep_warm_without_session(cfg, fake_cu):
    o = FakeOllama(cfg)
    res = core.look_at_screen("q", "Claude", cfg, o.client, keep_warm=True)
    assert o.keep_alives == ["10m"] and res["memory"]["reason"] == "keep_warm"


def test_pressure_unloads_after_answer(cfg, fake_cu, monkeypatch):
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 2000.0)
    o = FakeOllama(cfg)
    res = core.look_at_screen("q", "Claude", cfg, o.client, client="codex", session_id="s")
    assert res["summary"] == "done"                  # answered first
    assert res["memory"]["unloaded_low_memory"] is True and o.unloads == 1 and not o.loaded


def test_no_unload_when_min_free_disabled(cfg, fake_cu, monkeypatch):
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 100.0)
    cfg.min_free_mb = 0
    o = FakeOllama(cfg)
    core.look_at_screen("q", "Claude", cfg, o.client)
    assert o.unloads == 0


def test_unknown_free_ram_is_not_pressure(cfg, monkeypatch):
    assert memory.under_pressure(cfg, None) is False or memory.free_ram_mb() is not None
    monkeypatch.setattr(memory, "free_ram_mb", lambda: None)
    assert memory.under_pressure(cfg) is False


def test_release_model_reports_freed(cfg):
    o = FakeOllama(cfg, loaded=True)
    r = core.release_model(cfg, o.client)
    assert r["released"] is True and r["was_loaded"] is True
    assert r["freed_mb"] == 11 * 1024 and o.unloads == 1


def test_release_model_when_not_loaded(cfg):
    o = FakeOllama(cfg, loaded=False)
    r = core.release_model(cfg, o.client)
    assert r["was_loaded"] is False and r["freed_mb"] == 0.0 and o.unloads == 0


def test_release_model_ollama_down(cfg):
    def down(req):
        raise httpx.ConnectError("refused", request=req)
    c = OllamaClient(cfg, transport=httpx.MockTransport(down))
    r = core.release_model(cfg, c)
    assert r["released"] is False and r["freed_mb"] == 0.0 and "error" in r


def test_watcher_unloads_only_under_pressure(cfg, monkeypatch):
    o = FakeOllama(cfg, loaded=True)
    w = memory.MemoryWatcher(cfg, o.client, interval=0.01)
    assert w.check_once() is False and o.loaded
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 1000.0)
    assert w.check_once() is True and not o.loaded and w.unloads == 1
    assert w.check_once() is False  # nothing loaded any more


def test_watcher_thread_runs(cfg, monkeypatch):
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 1000.0)
    o = FakeOllama(cfg, loaded=True)
    w = memory.MemoryWatcher(cfg, o.client, interval=0.01).start()
    deadline = time.time() + 2
    while o.loaded and time.time() < deadline:
        time.sleep(0.01)
    w.stop()
    assert not o.loaded


def test_health_reports_memory_policy(cfg):
    o = FakeOllama(cfg, loaded=True)
    h = core.health(cfg, o.client)
    m = h["memory"]
    assert h["model_loaded"] is True and m["free_ram_mb"] == 16000.0
    assert m["keep_alive_next_call"] == "3m" and m["keep_alive_session"] == "10m"
    assert m["under_pressure"] is False and m["memory_watch"] is True


def test_config_env_memory():
    c = load_config(env={"MCP_CU_KEEP_ALIVE_IDLE": "1m", "MCP_CU_KEEP_ALIVE_SESSION": "20m",
                         "MCP_CU_MIN_FREE_MB": "2048", "MCP_CU_MEMORY_WATCH": "0"})
    assert (c.keep_alive_idle, c.keep_alive_session, c.min_free_mb, c.memory_watch) == ("1m", "20m", 2048, False)


def test_real_free_ram_reader_returns_number_or_none():
    v = REAL_FREE_RAM()  # imported before the autouse patch
    assert v is None or v > 0


def test_default_min_free_is_2048(cfg, fake_cu, monkeypatch):
    from mcp_computer_use.config import Config
    assert Config().min_free_mb == 2048
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 3800.0)  # above 2 GB: stay loaded
    o = FakeOllama(cfg)
    res = core.look_at_screen("q", "Claude", cfg, o.client, client="codex", session_id="s")
    assert o.unloads == 0 and "unloaded_low_memory" not in res["memory"]
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 1900.0)
    core.look_at_screen("q2", "Claude", cfg, o.client, client="codex", session_id="s")
    assert o.unloads == 1
