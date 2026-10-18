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
        except Exception:  # CuError, AttributeError, TypeError from malformed /api/ps
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

    def ping_model(self, timeout: float, keep_alive: str | int | None = None) -> str:
        """Tiny round-trip: ask the model to reply 'pong'. Returns the reply text."""
        model = self.cfg.model
        body = {"model": model, "stream": False,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "options": {"num_predict": 128, "temperature": 0.0, "num_ctx": 1024},
                "think": "low" if model.startswith("gpt-oss") else False,
                "keep_alive": keep_alive if keep_alive is not None else self.cfg.keep_alive_idle}
        try:
            with self._client(timeout) as c:
                r = c.post("/api/chat", json=body)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise CuError(MODEL_TIMEOUT, f"{model} did not answer the ping within {timeout}s")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise CuError(OLLAMA_UNREACHABLE, f"Ollama at {self.cfg.ollama_url} unreachable: {e}")
        except httpx.HTTPError as e:
            raise CuError(MODEL_FAILED, f"Ollama request failed: {e}")
        if r.status_code == 404:
            raise CuError(MODEL_NOT_FOUND, f"model '{model}' not found in Ollama at {self.cfg.ollama_url}")
        if r.status_code >= 400:
            raise CuError(MODEL_FAILED, f"Ollama HTTP {r.status_code}: {r.text[:200]}")
        return str((r.json().get("message") or {}).get("content", "")).strip()

    def _do_chat_request(self, body: dict, timeout: float) -> tuple[dict, dict]:
        """Send /api/chat and return (parsed_answer_dict, raw_response_data).

        Raises CuError on any transport failure, non-2xx status, or malformed
        response envelope (body not JSON, message/content not accessible).
        Does NOT compute stats — callers do that so they can interleave their
        own validation before stat parsing.
        """
        model = self.cfg.model
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
            raise CuError(MODEL_NOT_FOUND,
                          f"model '{model}' not found in Ollama at {self.cfg.ollama_url}")
        if r.status_code >= 400:
            raise CuError(MODEL_FAILED, f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        # Parse the response envelope; convert any malformed structure to CuError.
        try:
            data = r.json()
            content = str((data.get("message") or {}).get("content", "")).strip()
        except Exception as exc:
            raise CuError(MODEL_FAILED, f"Ollama response envelope malformed: {exc}") from exc
        try:
            answer = json.loads(content)
        except json.JSONDecodeError:
            raise CuError(MODEL_FAILED, f"model returned non-JSON output: {content[:200]!r}")
        return answer, data

    @staticmethod
    def _compute_stats(data: dict) -> dict:
        """Compute the stats dict from a raw response dict; raises CuError on bad types."""
        try:
            ev = int(data.get("eval_count") or 0)
            evd = int(data.get("eval_duration") or 0)
            return {
                "eval_tokens": ev,
                "tokens_per_second": round(ev / (evd / 1e9), 1) if evd else None,
                "model_seconds": round(int(data.get("total_duration") or 0) / 1e9, 2),
            }
        except Exception as exc:
            raise CuError(MODEL_FAILED, f"Ollama stats malformed: {exc}") from exc

    def _make_chat_body(self, system: str, user: str, schema: dict,
                        keep_alive: str | int | None, num_predict: int | None) -> dict:
        """Build the /api/chat request body."""
        model = self.cfg.model
        options: dict = {"num_ctx": self.cfg.num_ctx, "temperature": self.cfg.temperature}
        if num_predict is not None:
            options["num_predict"] = num_predict
        return {
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": schema,
            "options": options,
            # gpt-oss only supports think levels; other models: disable thinking.
            "think": "low" if model.startswith("gpt-oss") else False,
            "keep_alive": keep_alive if keep_alive is not None else self.cfg.keep_alive_idle,
        }

    def chat_schema(self, system: str, user: str, schema: dict, timeout: float,
                    keep_alive: str | int | None = None,
                    num_predict: int | None = None) -> tuple[dict, dict]:
        """Send a structured-output chat request and return (parsed_dict, stats).

        Uses Ollama's ``format`` parameter to enforce *schema*.  The response
        must be valid JSON; schema-level validation (required keys, types, …)
        is left to the caller.  Raises CuError on any network / model failure,
        including malformed response envelopes and invalid stats fields.

        *num_predict* is optional; when provided it is added to ``options``.
        """
        body = self._make_chat_body(system, user, schema, keep_alive, num_predict)
        answer, data = self._do_chat_request(body, timeout)
        stats = self._compute_stats(data)
        return answer, stats

    def chat_json(self, system: str, user: str, timeout: float,
                  keep_alive: str | int | None = None) -> tuple[dict, dict]:
        """Returns (parsed_answer, stats). Raises CuError on any failure.

        The ``summary`` field check is performed **before** stats parsing,
        preserving the original ordering: a missing summary always raises
        MODEL_FAILED regardless of whether stats fields are also malformed.
        """
        body = self._make_chat_body(system, user, ANSWER_SCHEMA, keep_alive, None)
        answer, data = self._do_chat_request(body, timeout)
        # chat_json contract: answer must contain "summary".
        # This happens before stats — original ordering restored.
        if not isinstance(answer, dict) or "summary" not in answer:
            content_repr = repr(str(answer)[:200])
            raise CuError(MODEL_FAILED, f"model JSON missing 'summary': {content_repr}")
        stats = self._compute_stats(data)
        return answer, stats

