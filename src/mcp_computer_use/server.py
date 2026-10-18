"""MCP server entry point (stdio), with screen reading and app-scoped actions."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # mcp 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

from typing import Annotated

from pydantic import Field

from . import core
from .guidance import (INSTRUCTIONS, LOOK_DESCRIPTION, HEALTH_DESCRIPTION,
                       QUESTION_DESCRIPTION, APP_DESCRIPTION, CLIENT_DESCRIPTION, SESSION_DESCRIPTION,
                       RELEASE_DESCRIPTION, KEEP_WARM_DESCRIPTION, PING_DESCRIPTION,
                       CLICK_DESCRIPTION, TYPE_DESCRIPTION, TARGET_DESCRIPTION, TEXT_DESCRIPTION, CONFIRM_DESCRIPTION)
from .memory import MemoryWatcher
from .ollama_client import OllamaClient
from .config import Config, load_config



def build_server(cfg: Config, ollama: OllamaClient | None = None) -> _Server:
    ollama = ollama if ollama is not None else OllamaClient(cfg)
    mcp = _Server("mcp-computer-use", instructions=INSTRUCTIONS)

    @mcp.tool(description=LOOK_DESCRIPTION)
    def look_at_screen(
        question: Annotated[str, Field(description=QUESTION_DESCRIPTION)],
        app: Annotated[str | None, Field(description=APP_DESCRIPTION)] = None,
        client: Annotated[str | None, Field(description=CLIENT_DESCRIPTION)] = None,
        session_id: Annotated[str | None, Field(description=SESSION_DESCRIPTION)] = None,
        keep_warm: Annotated[bool, Field(description=KEEP_WARM_DESCRIPTION)] = False,
    ) -> dict:
        return core.look_at_screen(question, app, cfg, ollama=ollama, client=client, session_id=session_id,
                                   keep_warm=bool(keep_warm))

    @mcp.tool(description=CLICK_DESCRIPTION)
    def click(
        app: Annotated[str, Field(description=APP_DESCRIPTION)],
        target: Annotated[str, Field(description=TARGET_DESCRIPTION)],
        client: Annotated[str | None, Field(description=CLIENT_DESCRIPTION)] = None,
        session_id: Annotated[str | None, Field(description=SESSION_DESCRIPTION)] = None,
        confirm_dangerous: Annotated[bool, Field(description=CONFIRM_DESCRIPTION)] = False,
    ) -> dict:
        return core.click(app, target, cfg, ollama=ollama, client=client, session_id=session_id,
                          confirm_dangerous=confirm_dangerous)

    @mcp.tool(description=TYPE_DESCRIPTION)
    def type_text(
        app: Annotated[str, Field(description=APP_DESCRIPTION)],
        target: Annotated[str, Field(description=TARGET_DESCRIPTION)],
        text: Annotated[str, Field(description=TEXT_DESCRIPTION)],
        client: Annotated[str | None, Field(description=CLIENT_DESCRIPTION)] = None,
        session_id: Annotated[str | None, Field(description=SESSION_DESCRIPTION)] = None,
        confirm_dangerous: Annotated[bool, Field(description=CONFIRM_DESCRIPTION)] = False,
    ) -> dict:
        return core.type_text(app, target, text, cfg, ollama=ollama, client=client, session_id=session_id,
                              confirm_dangerous=confirm_dangerous)

    @mcp.tool(description=PING_DESCRIPTION)
    def ping() -> dict:
        return core.ping(cfg, ollama=ollama)

    @mcp.tool(description=RELEASE_DESCRIPTION)
    def release_model() -> dict:
        return core.release_model(cfg, ollama=ollama)

    @mcp.tool(description=HEALTH_DESCRIPTION)
    def health() -> dict:
        return core.health(cfg, ollama=ollama)

    return mcp


def start_health_http(cfg: Config) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != "/health":
                self.send_error(404)
                return
            body = json.dumps(core.health(cfg), ensure_ascii=False).encode("utf-8")
            self.send_response(200 if json.loads(body)["ok"] else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep stdout clean for MCP stdio
            pass

    httpd = ThreadingHTTPServer((cfg.health_host, cfg.health_port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"health endpoint: http://{cfg.health_host}:{cfg.health_port}/health", file=sys.stderr)
    return httpd


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mcp-computer-use")
    p.add_argument("--config", help="path to a TOML config file")
    p.add_argument("--health", action="store_true", help="print health JSON and exit")
    p.add_argument("--ask", metavar="QUESTION", help="run look_at_screen once and print JSON")
    p.add_argument("--app", help="app for --ask")
    p.add_argument("--click", nargs=2, metavar=("APP", "TARGET"), help="click one visible label")
    p.add_argument("--type", nargs=3, metavar=("APP", "TARGET", "TEXT"), help="set an editable field value")
    p.add_argument("--confirm-dangerous", action="store_true", help="confirm only after explicit human approval")
    p.add_argument("--client", help="optional client name for screen reading and actions")
    p.add_argument("--session-id", help="optional session id (context for --ask; audit label for actions)")
    p.add_argument("--keep-warm", action="store_true", help="--ask: keep the model loaded 10 min")
    p.add_argument("--ping", action="store_true", help="heartbeat: server + Ollama (auto-start) + model 'pong'")
    p.add_argument("--release", action="store_true", help="unload the model now and print freed memory")
    p.add_argument("--health-http", action="store_true", help="also serve GET /health over HTTP")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if a.health_http:
        cfg.health_http = True
    def out(d: dict) -> None:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # Hebrew on Windows consoles
        except Exception:
            pass
        print(json.dumps(d, ensure_ascii=False, indent=2))
    if a.click:
        return out(core.click(*a.click, cfg, client=a.client, session_id=a.session_id,
                              confirm_dangerous=a.confirm_dangerous))
    if a.type:
        return out(core.type_text(*a.type, cfg, client=a.client, session_id=a.session_id,
                                  confirm_dangerous=a.confirm_dangerous))
    if a.health:
        return out(core.health(cfg))
    if a.ask:
        return out(core.look_at_screen(a.ask, a.app, cfg, client=a.client, session_id=a.session_id,
                                            keep_warm=a.keep_warm))
    if a.ping:
        return out(core.ping(cfg))
    if a.release:
        return out(core.release_model(cfg))
    if cfg.health_http:
        start_health_http(cfg)
    ollama = OllamaClient(cfg)
    if cfg.memory_watch:
        MemoryWatcher(cfg, ollama).start()
    build_server(cfg, ollama=ollama).run("stdio")


if __name__ == "__main__":
    main()
