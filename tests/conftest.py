import json
import subprocess

import httpx
import pytest

from mcp_computer_use import agent_cu
from mcp_computer_use.config import Config
from mcp_computer_use.ollama_client import OllamaClient

CLAUDE_TEXT = """Claude
New chat
You said: run the tests and push
run the tests and push
checked
I ran the whole suite.
Ran 3 commands
All 916 UI tests pass and 52 Rust crates pass.
Used Bash
Pushed commit be8b48e and built a new installer.
unchecked
Claude finished the response
How is Claude doing this session?
New session
"""


@pytest.fixture(autouse=True)
def _no_real_ollama_spawn(monkeypatch):
    from mcp_computer_use import autostart
    def boom(*a, **k):
        raise AssertionError("test tried to spawn a real ollama")
    monkeypatch.setattr(autostart, "_spawn", boom)
    autostart.state.update(autostarted=False, last=None)
    yield
    autostart.state.update(autostarted=False, last=None)


@pytest.fixture(autouse=True)
def _plenty_of_ram(monkeypatch):
    from mcp_computer_use import memory
    monkeypatch.setattr(memory, "free_ram_mb", lambda: 16000.0)


@pytest.fixture(autouse=True)
def _reset_stateless_hint():
    from mcp_computer_use import core
    core._stateless_calls.clear()
    yield
    core._stateless_calls.clear()


@pytest.fixture
def cfg(tmp_path):
    return Config(ollama_url="http://ollama.test:11435", model="gpt-oss:20b",
                  sessions_dir=str(tmp_path / ".sessions"))


class FakeAgentCu:
    """Mocks subprocess.run for agent-cu."""

    def __init__(self, apps=None, texts=None, fail=None, snaps=None):
        self.snaps = snaps or {}
        self.apps = apps if apps is not None else [{"name": "Claude", "frontmost": False},
                                                   {"name": "ChatGPT", "frontmost": True}]
        self.texts = texts or {"Claude": CLAUDE_TEXT, "ChatGPT": "Codex\nhello"}
        self.fail = fail  # None | "timeout" | "error"
        self.calls = []
        self.clicked = {}
        self.typed = {}

    def __call__(self, args, **kw):
        self.calls.append(args[1:])
        if self.fail == "timeout":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        if self.fail == "error":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        sub = args[1]
        if sub == "apps":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.apps), "")
        if sub == "text":
            app = args[3]
            if app not in self.texts:
                return subprocess.CompletedProcess(args, 1, "", f"App not found: {app}")
            return subprocess.CompletedProcess(args, 0, self.texts[app], "")
        if sub == "snapshot":
            app = args[3]
            if app not in self.snaps:
                return subprocess.CompletedProcess(args, 1, "", "snapshot unavailable")
            return subprocess.CompletedProcess(args, 0, self.snaps[app], "")
        if sub == "click":
            app = args[args.index("-a") + 1]
            self.clicked.setdefault(app, []).append(args[2])
            return subprocess.CompletedProcess(args, 0, "clicked", "")
        if sub == "type":
            assert args[2] == "-s" and args[6] == "--"
            assert "--submit" not in args[:7]
            app = args[args.index("-a") + 1]
            self.typed.setdefault(app, []).append((args[3], args[7]))
            return subprocess.CompletedProcess(args, 0, "typed", "")
        raise AssertionError(f"unexpected agent-cu call {args}")


@pytest.fixture
def fake_cu(monkeypatch):
    fake = FakeAgentCu()
    monkeypatch.setattr(agent_cu, "resolve_cmd", lambda cmd: "/usr/bin/agent-cu")
    monkeypatch.setattr(agent_cu.subprocess, "run", fake)
    return fake


def make_ollama(cfg, chat=None, loaded=True, present=True, unreachable=False, record=None):
    def handler(req: httpx.Request):
        if unreachable:
            raise httpx.ConnectError("refused", request=req)
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}] if present else []})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b", "size": 13 * 2**30,
                                                         "size_vram": 11 * 2**30}] if loaded else []})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if record is not None:
                record.append(body)
            if callable(chat):
                return chat(req, body)
            return httpx.Response(200, json={
                "message": {"content": json.dumps(chat or {"status": "finished",
                            "summary": "Claude finished: 916 tests pass, pushed be8b48e.", "confidence": 0.9})},
                "eval_count": 60, "eval_duration": 5_000_000_000, "total_duration": 6_000_000_000})
        return httpx.Response(404)
    return OllamaClient(cfg, transport=httpx.MockTransport(handler))
