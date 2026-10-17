"""Structured errors returned to the calling agent instead of raising."""
from __future__ import annotations

OLLAMA_UNREACHABLE = "OLLAMA_UNREACHABLE"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_FAILED = "MODEL_FAILED"
AGENT_CU_NOT_INSTALLED = "AGENT_CU_NOT_INSTALLED"
AGENT_CU_FAILED = "AGENT_CU_FAILED"
APP_NOT_FOUND = "APP_NOT_FOUND"
QUESTION_TOO_LONG = "QUESTION_TOO_LONG"
EMPTY_QUESTION = "EMPTY_QUESTION"

_DEFAULTS = {
    OLLAMA_UNREACHABLE: (True, "The server already tried to auto-start a local Ollama (MCP_CU_AUTOSTART_OLLAMA). Call health() to confirm. Start Ollama (e.g. `$env:OLLAMA_HOST='127.0.0.1:11435'; ollama serve`) or fix MCP_CU_OLLAMA_URL. Meanwhile use the raw `extracted` text."),
    MODEL_NOT_FOUND: (False, "Run `ollama pull <model>` against the configured Ollama URL, or set MCP_CU_MODEL."),
    MODEL_TIMEOUT: (True, "Retry once (the model may have been cold-loading); if it fails again use the raw `extracted` text."),
    MODEL_FAILED: (True, "Use the raw `extracted` text, or retry."),
    AGENT_CU_NOT_INSTALLED: (False, "Install with `npm i -g agent-cu` or set MCP_CU_AGENT_CU_CMD to its full path."),
    AGENT_CU_FAILED: (True, "Retry; make sure the target window is open and not minimized."),
    APP_NOT_FOUND: (False, "Call again with an explicit `app` from `available_apps`."),
    QUESTION_TOO_LONG: (True, "Split it into shorter, focused English questions and call look_at_screen once per step (first status, then details)."),
    EMPTY_QUESTION: (True, "Pass one short English question, e.g. 'Is Claude working or finished?'."),
}


class CuError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def error_payload(code: str, message: str, **extra) -> dict:
    retryable, suggestion = _DEFAULTS.get(code, (False, ""))
    out = {"error": {"code": code, "message": message,
                     "retryable": retryable, "suggestion": suggestion}}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out
