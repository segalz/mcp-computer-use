"""MCP server entry point (stdio). Read-only: exposes look_at_screen and health only."""
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
                       QUESTION_DESCRIPTION, APP_DESCRIPTION)
from .config import Config, load_config



def build_server(cfg: Config) -> _Server:
    mcp = _Server("mcp-computer-use", instructions=INSTRUCTIONS)

    @mcp.tool(description=LOOK_DESCRIPTION)
    def look_at_screen(
        question: Annotated[str, Field(description=QUESTION_DESCRIPTION)],
        app: Annotated[str | None, Field(description=APP_DESCRIPTION)] = None,
    ) -> dict:
        return core.look_at_screen(question, app, cfg)

    @mcp.tool(description=HEALTH_DESCRIPTION)
    def health() -> dict:
        return core.health(cfg)

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
    if a.health:
        return out(core.health(cfg))
    if a.ask:
        return out(core.look_at_screen(a.ask, a.app, cfg))
    if cfg.health_http:
        start_health_http(cfg)
    build_server(cfg).run("stdio")


if __name__ == "__main__":
    main()
