"""Goal A: optional session files."""
import json
import os
import time

from mcp_computer_use import core, server
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.guidance import LOOK_DESCRIPTION
from mcp_computer_use.sessions import Session, cleanup, sanitize, sessions_dir

from conftest import make_ollama, CLAUDE_TEXT


def files(cfg):
    d = sessions_dir(cfg)
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def test_sanitize():
    assert sanitize("co dex!/..") == "codex"
    assert sanitize("a" * 100) == "a" * 64
    assert sanitize("***") is None and sanitize(None) is None
    assert sanitize("fix-ui_2") == "fix-ui_2"


def test_without_session_no_file_and_no_changes(cfg, fake_cu):
    res = core.look_at_screen("Is Claude finished?", "Claude", cfg, make_ollama(cfg))
    assert "changed_since_last" not in res and "session" not in res
    assert files(cfg) == []
    assert "hint" not in res


def test_repeated_stateless_calls_get_hint(cfg, fake_cu):
    core.look_at_screen("Is Claude finished?", "Claude", cfg, make_ollama(cfg), client="codex")
    res = core.look_at_screen("What did Claude write?", "Claude", cfg, make_ollama(cfg), client="codex")
    assert "session_id" in res["hint"]
    assert files(cfg) == []


def test_session_file_created_and_updated(cfg, fake_cu):
    sent = []
    o = make_ollama(cfg, record=sent)
    r1 = core.look_at_screen("Is Claude finished?", "Claude", cfg, o, client="codex", session_id="bench")
    assert r1["changed_since_last"] == ["first call in this session"]
    assert r1["session"] == {"client": "codex", "session_id": "bench"}
    assert files(cfg) == ["codex__bench.json"]
    data = json.loads((sessions_dir(cfg) / "codex__bench.json").read_text(encoding="utf-8"))
    for k in ("app", "status", "last_message", "questions", "updated_at"):
        assert k in data
    assert data["questions"][0]["q"] == "Is Claude finished?"

    fake_cu.texts["Claude"] = CLAUDE_TEXT.replace("Claude finished the response", "Claude is responding") \
                                         .replace("built a new installer.", "built a new installer.\nNow deploying.")
    r2 = core.look_at_screen("What is new?", "Claude", cfg, o, client="codex", session_id="bench")
    assert "status: finished -> working" in r2["changed_since_last"]
    assert "new text in the last message" in r2["changed_since_last"]
    user = sent[-1]["messages"][1]["content"]
    assert "<changed_since_last>" in user and "<previous_questions>" in user
    data = json.loads((sessions_dir(cfg) / "codex__bench.json").read_text(encoding="utf-8"))
    assert [q["q"] for q in data["questions"]] == ["Is Claude finished?", "What is new?"]


def test_unchanged_screen_same_question_is_cached(cfg, fake_cu):
    sent = []
    o = make_ollama(cfg, record=sent)
    core.look_at_screen("Is Claude finished?", "Claude", cfg, o, client="codex", session_id="s1")
    r = core.look_at_screen("Is Claude finished?", "Claude", cfg, o, client="codex", session_id="s1")
    assert len(sent) == 1 and r["cached"] is True
    assert r["changed_since_last"] == ["nothing changed"] and r["summary"]


def test_new_session_id_replaces_old_for_same_client_only(cfg, fake_cu):
    o = make_ollama(cfg)
    core.look_at_screen("q", "Claude", cfg, o, client="codex", session_id="a")
    core.look_at_screen("q", "Claude", cfg, o, client="claude", session_id="x")
    r = core.look_at_screen("q", "Claude", cfg, o, client="codex", session_id="b")
    assert files(cfg) == ["claude__x.json", "codex__b.json"]
    assert r["session"]["replaced"] == ["a"]


def test_session_without_client_uses_default(cfg, fake_cu):
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg), session_id="t")
    assert files(cfg) == ["default__t.json"]


def test_invalid_session_id_runs_stateless(cfg, fake_cu):
    r = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg), client="codex", session_id="!!!")
    assert "session" not in r and files(cfg) == [] and "no valid characters" in r["hint"]


def test_ttl_cleanup(cfg, fake_cu):
    d = sessions_dir(cfg)
    d.mkdir(parents=True)
    old = d / "other__old.json"
    old.write_text("{}")
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg), client="codex", session_id="n")
    assert files(cfg) == ["codex__n.json"]
    assert cleanup(d, 0) == 0  # ttl 0 disables cleanup


def test_config_env(tmp_path):
    cfg = load_config(env={"MCP_CU_SESSIONS_DIR": str(tmp_path), "MCP_CU_SESSION_TTL_HOURS": "2"})
    assert cfg.sessions_dir == str(tmp_path) and cfg.session_ttl_hours == 2.0
    assert sessions_dir(Config()).name == ".sessions"


def test_tool_schema_and_description():
    import asyncio
    tools = asyncio.run(server.build_server(Config()).list_tools())
    look = next(t for t in tools if t.name == "look_at_screen")
    schema = getattr(look, "inputSchema", None) or getattr(look, "input_schema")
    props = schema["properties"]
    assert {"client", "session_id"} <= props.keys()
    assert set(schema.get("required", [])) == {"question"}
    for phrase in ("OPTIONAL", "independent", "no file is written", "replaces"):
        assert phrase in LOOK_DESCRIPTION
