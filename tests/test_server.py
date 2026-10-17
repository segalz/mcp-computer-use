import asyncio
import json
import urllib.request

from mcp_computer_use import server, core
from mcp_computer_use.config import Config


def test_tools_registered_read_only():
    mcp = server.build_server(Config())
    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == ["health", "look_at_screen", "ping", "release_model"]  # no click/type tools


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
