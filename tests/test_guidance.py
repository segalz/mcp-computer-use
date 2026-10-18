import asyncio

from mcp_computer_use import core, server
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.guidance import (INSTRUCTIONS, HINT_ENGLISH, HINT_MULTIPART,
                                       is_multipart, mostly_non_latin)

from conftest import make_ollama


def test_question_too_long(cfg, fake_cu):
    res = core.look_at_screen("Is Claude done? " * 30, "Claude", cfg, make_ollama(cfg))
    assert res["error"]["code"] == "QUESTION_TOO_LONG"
    assert "split" in res["error"]["suggestion"].lower()
    assert fake_cu.calls == []  # rejected before touching the screen


def test_question_limit_configurable(cfg, fake_cu):
    cfg.max_question_chars = 10
    assert core.look_at_screen("Is Claude finished?", "Claude", cfg, make_ollama(cfg))["error"]["code"] == "QUESTION_TOO_LONG"
    assert load_config(path="/none", env={"MCP_CU_MAX_QUESTION_CHARS": "500"}).max_question_chars == 500
    assert Config().max_question_chars == 300


def test_empty_question(cfg, fake_cu):
    assert core.look_at_screen("   ", "Claude", cfg, make_ollama(cfg))["error"]["code"] == "EMPTY_QUESTION"


def test_focused_english_question_has_no_hint(cfg, fake_cu):
    res = core.look_at_screen("Is Claude working or finished?", "Claude", cfg, make_ollama(cfg))
    assert "error" not in res and "hint" not in res


def test_multipart_hint_still_answers(cfg, fake_cu):
    res = core.look_at_screen("Is it done? Which tests? What commit?", "Claude", cfg, make_ollama(cfg))
    assert res["summary"] and res["hint"] == HINT_MULTIPART
    res = core.look_at_screen("Check: 1. status 2. last message", "Claude", cfg, make_ollama(cfg))
    assert HINT_MULTIPART in res["hint"]


def test_hebrew_hint_still_answers(cfg, fake_cu):
    res = core.look_at_screen("מה קלוד עושה עכשיו?", None, cfg, make_ollama(cfg))
    assert res["windows_seen"] == ["Claude"] and res["hint"] == HINT_ENGLISH


def test_hint_on_error_too(cfg, fake_cu):
    res = core.look_at_screen("מה קלוד עושה?", "Claude", cfg, make_ollama(cfg, unreachable=True))
    assert res["error"]["code"] == "OLLAMA_UNREACHABLE" and "health()" in res["error"]["suggestion"]
    assert res["hint"] == HINT_ENGLISH


def test_detectors():
    assert is_multipart("a? b? c?") and not is_multipart("Is Claude done?")
    assert is_multipart("- status\n- last message")
    assert mostly_non_latin("מה קלוד עושה") and not mostly_non_latin("What is Claude doing (קלוד)?")
    assert not mostly_non_latin("123 ???")


def test_instructions_and_descriptions_registered():
    mcp = server.build_server(Config())
    assert mcp.instructions == INSTRUCTIONS
    for phrase in ("English", "step by step", "health()", "MODEL_TIMEOUT", "READ-ONLY"):
        assert phrase in INSTRUCTIONS
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    look = tools["look_at_screen"]
    assert "English" in look.description and "step by step" in look.description
    schema = getattr(look, "input_schema", None) or getattr(look, "inputSchema")
    props = schema["properties"]
    assert "max 300" in props["question"]["description"]
    assert "app" in props and "description" in props["app"]
    assert "OLLAMA_UNREACHABLE" in tools["health"].description



def test_action_guidance_registered_with_explicit_human_confirmation():
    from mcp_computer_use.guidance import CONFIRM_DESCRIPTION
    sentence = "You MUST ask the human user and get explicit approval before setting confirm_dangerous=true"
    assert sentence in CONFIRM_DESCRIPTION and sentence in INSTRUCTIONS
    assert "look_at_screen is a READ-ONLY" in INSTRUCTIONS
    assert "It cannot click" not in INSTRUCTIONS
    tools = {tool.name: tool for tool in asyncio.run(server.build_server(Config()).list_tools())}
    for name in ("click", "type_text"):
        tool = tools[name]
        assert sentence in tool.description
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
        props = schema["properties"]
        assert sentence in props["confirm_dangerous"]["description"]
        assert props["confirm_dangerous"]["default"] is False
        assert "visible label" in props["target"]["description"].lower()
        assert "refs change" in props["target"]["description"]
        assert "app" in schema["required"]
        if name == "type_text":
            assert "target" in schema["required"]
        else:
            assert "target" not in schema["required"]
    assert "text" in (getattr(tools["type_text"], "input_schema", None) or tools["type_text"].inputSchema)["required"]



def test_batch_click_guidance_registered():
    from mcp_computer_use.guidance import STEPS_DESCRIPTION
    tools = {tool.name: tool for tool in asyncio.run(server.build_server(Config()).list_tools())}
    tool = tools["click"]
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    steps = schema["properties"]["steps"]
    assert steps["default"] is None
    assert steps["description"] == STEPS_DESCRIPTION
    assert "10" in STEPS_DESCRIPTION and "safety gate" in STEPS_DESCRIPTION
    assert "known sequence" in STEPS_DESCRIPTION and "fresh snapshot" in STEPS_DESCRIPTION
    assert "stops" in STEPS_DESCRIPTION.lower()
    target = schema["properties"]["target"]
    assert {"type": "null"} in target["anyOf"]
    assert "steps" not in (getattr(tools["type_text"], "input_schema", None) or tools["type_text"].inputSchema)["properties"]



def test_click_schema_requires_only_app_and_lists_steps():
    tools = {tool.name: tool for tool in asyncio.run(server.build_server(Config()).list_tools())}
    schema = getattr(tools["click"], "input_schema", None) or tools["click"].inputSchema
    assert schema["required"] == ["app"]
    assert "steps" in schema["properties"]
    assert schema["properties"]["target"]["default"] is None


def test_guidance_length_limits_and_consistency():
    from mcp_computer_use import guidance

    # Tool descriptions <= ~900 chars; INSTRUCTIONS <= ~2500 chars
    assert len(guidance.INSTRUCTIONS) <= 2500, f"INSTRUCTIONS too long: {len(guidance.INSTRUCTIONS)}"
    for desc, name in [
        (guidance.LOOK_DESCRIPTION, "look_at_screen"),
        (guidance.CLICK_DESCRIPTION, "click"),
        (guidance.TYPE_DESCRIPTION, "type_text"),
        (guidance.PING_DESCRIPTION, "ping"),
        (guidance.RELEASE_DESCRIPTION, "release_model"),
        (guidance.HEALTH_DESCRIPTION, "health"),
    ]:
        assert len(desc) <= 900, f"{name} description too long: {len(desc)}"

    # INSTRUCTIONS mentions key tools, image option, and safety confirmation rule
    assert "click" in guidance.INSTRUCTIONS
    assert "type_text" in guidance.INSTRUCTIONS
    assert "include_image" in guidance.INSTRUCTIONS
    assert "You MUST ask the human user and get explicit approval before setting confirm_dangerous=true" in guidance.INSTRUCTIONS

    # Bench insight and per-app details keys mentioned in INSTRUCTIONS
    assert "bridge pays off" in guidance.INSTRUCTIONS
    assert "Claude Code" in guidance.INSTRUCTIONS
    assert "Codex desktop" in guidance.INSTRUCTIONS


def test_readme_snippet_matches_agents_snippet():
    from pathlib import Path
    agents_snippet = Path("docs/AGENTS_SNIPPET.md").read_text(encoding="utf-8").strip()
    readme = Path("README.md").read_text(encoding="utf-8")
    assert agents_snippet in readme

