import asyncio
import json
import urllib.request

from mcp_computer_use import server, core
from mcp_computer_use.config import Config


def test_tools_registered():
    mcp = server.build_server(Config())
    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == ["click", "health", "look_at_screen", "ping", "release_model", "type_text"]


def test_health_http(monkeypatch):
    monkeypatch.setattr(core, "health", lambda cfg: {"ok": True, "model": cfg.model})
    cfg = Config(health_port=0)
    httpd = server.start_health_http(cfg)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            assert r.status == 200 and json.loads(r.read())["ok"] is True
    finally:
        httpd.shutdown()


def test_look_at_screen_tool_unstructured_schema():
    mcp = server.build_server(Config())
    tools = asyncio.run(mcp.list_tools())
    look = next(t for t in tools if t.name == "look_at_screen")
    assert look.output_schema is None
    schema = getattr(look, "input_schema", None) or getattr(look, "inputSchema")
    assert "include_image" in schema["properties"]
    assert schema["properties"]["include_image"]["default"] is False
