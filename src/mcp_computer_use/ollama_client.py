"""Minimal Ollama HTTP client (/api/tags, /api/ps, /api/chat)."""
from __future__ import annotations

import json

import httpx

from .config import Config
from .errors import CuError, OLLAMA_UNREACHABLE, MODEL_TIMEOUT, MODEL_FAILED, MODEL_NOT_FOUND

# JSON schema enforced through Ollama's `format` parameter.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["working", "finished", "waiting_for_user", "error", "unknown"]},
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["status", "summary", "confidence"],
}


def _same_model(a: str, b: str) -> bool:
    norm = lambda s: s if ":" in s else s + ":latest"
    return norm(a) == norm(b)


class OllamaClient:
    def __init__(self, cfg: Config, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.cfg.ollama_url, timeout=timeout, transport=self._transport)

    def _get(self, path: str, timeout: float = 3.0) -> dict:
        try:
            with self._client(timeout) as c:
                r = c.get(path)
                r.raise_for_status()
                return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise CuError(OLLAMA_UNREACHABLE, f"Ollama at {self.cfg.ollama_url} unreachable: {e}")
        except httpx.HTTPError as e:
            raise CuError(OLLAMA_UNREACHABLE, f"Ollama {path} failed: {e}")

    def models(self) -> list[str]:
        return [m.get("name") or m.get("model", "") for m in self._get("/api/tags").get("models", [])]

    def running(self) -> list[dict]:
        return self._get("/api/ps").get("models", [])

    def model_memory_mb(self, model: str | None = None) -> float | None:
        model = model or self.cfg.model
        try:
            for m in self.running():
                if _same_model(m.get("name") or m.get("model", ""), model):
                    size = m.get("size_vram") or m.get("size") or 0
                    return round(size / (1024 * 1024), 1) if size else None
        except CuError:
            return None
        return None

    def is_loaded(self, model: str | None = None) -> bool:
        model = model or self.cfg.model
        try:
            return any(_same_model(m.get("name") or m.get("model", ""), model) for m in self.running())
        except CuError:
            return False

    def has_model(self, model: str | None = None) -> bool:
        model = model or self.cfg.model
        return any(_same_model(n, model) for n in self.models())

    def unload(self, timeout: float = 15.0) -> None:
        """Unload the model now (keep_alive=0 with an empty prompt)."""
        try:
            with self._client(timeout) as c:
                r = c.post("/api/generate", json={"model": self.cfg.model, "prompt": "", "keep_alive": 0})
        except httpx.HTTPError as e:
            raise CuError(OLLAMA_UNREACHABLE, f"unload failed: {e}")
        if r.status_code >= 400:
            raise CuError(MODEL_FAILED, f"unload HTTP {r.status_code}: {r.text[:200]}")

    def chat_json(self, system: str, user: str, timeout: float,
                  keep_alive: str | int | None = None) -> tuple[dict, dict]:
        """Returns (parsed_answer, stats). Raises CuError on any failure."""
        model = self.cfg.model
        body = {
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": ANSWER_SCHEMA,
            "options": {"num_ctx": self.cfg.num_ctx, "temperature": self.cfg.temperature},
            # gpt-oss only supports think levels; other models: disable thinking.
            "think": "low" if model.startswith("gpt-oss") else False,
            "keep_alive": keep_alive if keep_alive is not None else self.cfg.keep_alive_idle,
        }
        try:
            with self._client(timeout) as c:
                r = c.post("/api/chat", json=body)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise CuError(MODEL_TIMEOUT, f"{model} did not answer within {timeout}s")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise CuError(OLLAMA_UNREACHABLE, f"Ollama at {self.cfg.ollama_url} unreachable: {e}")
        except httpx.HTTPError as e:
            raise CuError(MODEL_FAILED, f"Ollama request failed: {e}")
        if r.status_code == 404:
            raise CuError(MODEL_NOT_FOUND, f"model '{model}' not found in Ollama at {self.cfg.ollama_url}")
        if r.status_code >= 400:
            raise CuError(MODEL_FAILED, f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        content = (data.get("message") or {}).get("content", "").strip()
        try:
            answer = json.loads(content)
        except json.JSONDecodeError:
            raise CuError(MODEL_FAILED, f"model returned non-JSON output: {content[:200]!r}")
        if not isinstance(answer, dict) or "summary" not in answer:
            raise CuError(MODEL_FAILED, f"model JSON missing 'summary': {content[:200]!r}")
        ev, evd = data.get("eval_count") or 0, data.get("eval_duration") or 0
        stats = {
            "eval_tokens": ev,
            "tokens_per_second": round(ev / (evd / 1e9), 1) if evd else None,
            "model_seconds": round((data.get("total_duration") or 0) / 1e9, 2),
        }
        return answer, stats
