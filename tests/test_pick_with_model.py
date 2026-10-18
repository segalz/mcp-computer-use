"""Tests for pick_with_model (A5) and chat_schema (generalised chat_json)."""
from __future__ import annotations

import json
import unicodedata

import httpx
import pytest

from mcp_computer_use.agent_cu import Element
from mcp_computer_use.config import Config
from mcp_computer_use.errors import (
    CuError,
    MODEL_TIMEOUT,
    OLLAMA_UNREACHABLE,
    MODEL_FAILED,
)
from mcp_computer_use.matching import (
    pick_with_model,
    _SYSTEM_PICK,
    _REF_PICK_SCHEMA,
    _clean_field,
)
from mcp_computer_use.ollama_client import OllamaClient, ANSWER_SCHEMA

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_elements(*names, role="button"):
    return [Element(f"@e{i}", role, name) for i, name in enumerate(names, 1)]


def _make_ollama_pick(cfg, ref_reply, record=None, error=None, loaded=True,
                      timeout_record=None):
    """Return an OllamaClient whose /api/chat returns {ref: ref_reply}.

    *timeout_record*, if provided, is a list that will have the httpx client
    timeout appended when a /api/chat request is made.
    """

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            models = [{"name": "gpt-oss:20b"}] if loaded else []
            return httpx.Response(200, json={"models": models})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if record is not None:
                record.append(body)
            if error == "timeout":
                raise httpx.ReadTimeout("timeout", request=req)
            if error == "unreachable":
                raise httpx.ConnectError("refused", request=req)
            payload = {"ref": ref_reply}
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": 10,
                "eval_duration": 1_000_000_000,
                "total_duration": 1_200_000_000,
            })
        return httpx.Response(404)

    return OllamaClient(cfg, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# cfg fixture with resolve_with_model=True (default)
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_resolve(tmp_path):
    return Config(
        ollama_url="http://ollama.test:11435",
        model="gpt-oss:20b",
        sessions_dir=str(tmp_path / ".sessions"),
        resolve_with_model=True,
        timeout_base=10.0,
        cold_load_extra=45.0,
    )


@pytest.fixture
def cfg_no_resolve(tmp_path):
    return Config(
        ollama_url="http://ollama.test:11435",
        model="gpt-oss:20b",
        sessions_dir=str(tmp_path / ".sessions"),
        resolve_with_model=False,
    )


# ===========================================================================
# Basic happy-path and rejection cases
# ===========================================================================

def test_valid_ref_returns_element(cfg_resolve):
    cands = _make_elements("Save", "Close", "Open")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e2")
    result = pick_with_model("Close", cands, ollama, cfg_resolve)
    assert result is cands[1]
    assert result.ref == "@e2"


def test_null_ref_returns_none(cfg_resolve):
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply=None)
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_unlisted_ref_returns_none(cfg_resolve):
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e99")
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_model_timeout_returns_none(cfg_resolve):
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", error="timeout")
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_ollama_unreachable_returns_none(cfg_resolve):
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", error="unreachable")
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_resolve_with_model_false_no_model_call(cfg_no_resolve):
    called = []

    def handler(req: httpx.Request):
        called.append(req)
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_no_resolve, transport=httpx.MockTransport(handler))
    cands = _make_elements("Save", "Close")
    result = pick_with_model("Save", cands, ollama, cfg_no_resolve)
    # Must not have called /api/chat
    chat_calls = [r for r in called if "/api/chat" in r.url.path]
    assert chat_calls == []
    assert result is None


def test_malformed_json_returns_none(cfg_resolve):
    """If the model returns non-JSON content, chat_schema raises CuError → None."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={
                "message": {"content": "not json at all"},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(404)

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    cands = _make_elements("Save", "Close")
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


# ===========================================================================
# Non-string / unhashable ref values → None (blocker fix #1)
# ===========================================================================

def _make_ollama_with_ref_value(cfg, ref_value):
    """OllamaClient that returns the given Python value as the 'ref' field."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": cfg.model}]})
        if req.url.path == "/api/chat":
            payload = {"ref": ref_value}
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(404)

    return OllamaClient(cfg, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("bad_ref", [
    [],          # list — unhashable, would crash dict.get
    {},          # dict
    42,          # integer
    3.14,        # float
    True,        # bool
])
def test_non_string_ref_returns_none(cfg_resolve, bad_ref):
    """Non-string ref values (including unhashable) must → None, not crash."""
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_with_ref_value(cfg_resolve, bad_ref)
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


# ===========================================================================
# Malformed envelope → CuError / None  (blocker fix #2)
# ===========================================================================

def test_malformed_envelope_non_json_body_returns_none(cfg_resolve):
    """If the HTTP body itself is not JSON, chat_schema must raise CuError → None."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": cfg_resolve.model}]})
        if req.url.path == "/api/chat":
            # Return raw non-JSON bytes
            return httpx.Response(200, content=b"not-json-at-all",
                                   headers={"content-type": "text/plain"})
        return httpx.Response(404)

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    cands = _make_elements("Save", "Close")
    # Must not propagate JSONDecodeError — must return None
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_malformed_envelope_message_not_dict_returns_none(cfg_resolve):
    """If 'message' is a string (not a dict), chat_schema must raise CuError → None."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": cfg_resolve.model}]})
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={"message": "bad_string_not_dict"})
        return httpx.Response(404)

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    cands = _make_elements("Save", "Close")
    # "message" is a string, not dict; trying to get "content" returns None
    # and json.loads("") would raise → CuError → None
    assert pick_with_model("Save", cands, ollama, cfg_resolve) is None


def test_malformed_stats_raises_cu_error_from_chat_schema(cfg_resolve):
    """Stats fields with bad types must raise CuError(MODEL_FAILED), not TypeError."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            payload = {"status": "finished", "summary": "ok", "confidence": 0.9}
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": "not-an-int",   # bad type
                "eval_duration": 1_000_000_000,
                "total_duration": 2_000_000_000,
            })
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    with pytest.raises(CuError) as exc_info:
        ollama.chat_schema("sys", "usr", _REF_PICK_SCHEMA, timeout=10.0)
    assert exc_info.value.code == MODEL_FAILED


def test_malformed_ps_response_is_loaded_returns_false(cfg_resolve):
    """Malformed /api/ps (models is not a list of dicts) → is_loaded() returns False."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            # "models" is a string, not a list
            return httpx.Response(200, json={"models": "not-a-list"})
        return httpx.Response(404)

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    assert ollama.is_loaded() is False


# ===========================================================================
# Request shape assertions (think, format, prompt hygiene)
# ===========================================================================

def test_think_sent_as_low_for_gpt_oss(cfg_resolve):
    record = []
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Save", cands, ollama, cfg_resolve)
    assert len(record) == 1
    assert record[0]["think"] == "low", "gpt-oss model must send think='low'"


def test_think_sent_as_false_for_non_gpt_oss(tmp_path):
    cfg = Config(
        ollama_url="http://ollama.test:11435",
        model="llama3:8b",
        sessions_dir=str(tmp_path / ".sessions"),
        resolve_with_model=True,
    )
    record = []

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            record.append(body)
            return httpx.Response(200, json={
                "message": {"content": json.dumps({"ref": "@e1"})},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(404)

    ollama = OllamaClient(cfg, transport=httpx.MockTransport(handler))
    cands = _make_elements("Save", "Close")
    pick_with_model("Save", cands, ollama, cfg)
    assert len(record) == 1
    assert record[0]["think"] is False, "non-gpt-oss model must send think=False"


def test_format_equals_ref_pick_schema(cfg_resolve):
    record = []
    cands = _make_elements("Save", "Close")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Save", cands, ollama, cfg_resolve)
    assert record[0]["format"] == _REF_PICK_SCHEMA


def test_system_prompt_content(cfg_resolve):
    record = []
    cands = _make_elements("Save")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Save", cands, ollama, cfg_resolve)
    msgs = record[0]["messages"]
    system_msg = next(m for m in msgs if m["role"] == "system")
    assert "null" in system_msg["content"]
    assert "@eN" in system_msg["content"]
    # Must mention that data is untrusted
    assert "untrusted" in system_msg["content"].lower()


# ===========================================================================
# Prompt hygiene: ≤5 candidates, fields ≤80 chars, no control chars (JSON format)
# ===========================================================================

def _parse_prompt_candidates(user_content: str) -> list[dict]:
    """Parse the new JSON-line candidate format from the user prompt.

    First line is a JSON string (the target); remaining lines are JSON objects.
    """
    lines = user_content.splitlines()
    cand_lines = lines[1:]  # skip target JSON string
    result = []
    for line in cand_lines:
        line = line.strip()
        if line:
            result.append(json.loads(line))
    return result


def test_prompt_has_at_most_five_candidates(cfg_resolve):
    """Even if >5 candidates are passed, only ≤5 appear in the prompt."""
    record = []
    many = [Element(f"@e{i}", "button", f"Button {i}") for i in range(1, 12)]
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Button 1", many, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    candidates = _parse_prompt_candidates(user_content)
    assert len(candidates) <= 5


def test_prompt_fields_capped_at_80_chars(cfg_resolve):
    """Long names / context are truncated to 80 chars in the JSON objects."""
    long_name = "A" * 200
    long_ctx = "B" * 200
    cands = [Element("@e1", "button", long_name, context=long_ctx)]
    record = []
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("test", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    candidates = _parse_prompt_candidates(user_content)
    assert len(candidates) == 1
    assert len(candidates[0]["name"]) <= 80
    assert len(candidates[0]["ctx"]) <= 80


def test_prompt_no_control_chars_in_fields(cfg_resolve):
    """Control chars (\\n, \\r, \\x00) in element fields must be sanitised."""
    cands = [Element("@e1", "button\ninjected", "Save\nline2", context="Ctx\rline")]
    record = []
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Save", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    candidates = _parse_prompt_candidates(user_content)
    assert len(candidates) == 1
    # After sanitisation, no raw newlines should appear in the field values
    assert "\n" not in candidates[0]["name"]
    assert "\r" not in candidates[0]["name"]
    assert "\n" not in candidates[0]["role"]


def test_prompt_unicode_line_separators_stripped(cfg_resolve):
    """U+2028 (line separator) and U+2029 (paragraph separator) must be stripped."""
    ls = "\u2028"  # LINE SEPARATOR (Zl)
    ps = "\u2029"  # PARAGRAPH SEPARATOR (Zp)
    cands = [Element("@e1", "button", f"Sa{ls}ve{ps}As")]
    record = []
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model(f"Save{ls}As", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    # Neither U+2028 nor U+2029 should appear raw in the JSON prompt
    assert ls not in user_content
    assert ps not in user_content


def test_prompt_target_no_control_chars(cfg_resolve):
    """Control chars in the target itself must be stripped across the entire prompt."""
    record = []
    cands = _make_elements("Save")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model("Sa\nve\r\n", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    # The whole prompt (not just the first line) must not contain raw \n/\r
    # except the newline separating the JSON lines themselves.
    # Decode the first JSON string and verify no newlines inside it:
    lines = user_content.splitlines()
    target_value = json.loads(lines[0])
    assert "\n" not in target_value
    assert "\r" not in target_value


def test_prompt_target_unicode_line_sep_whole_prompt(cfg_resolve):
    """U+2028 in target must not appear anywhere in the prompt."""
    record = []
    ls = "\u2028"
    cands = _make_elements("Save")
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model(f"Sa{ls}ve", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    assert ls not in user_content


def test_clean_field_exhaustive_unicode_categories(cfg_resolve):
    """For every code point in range(0x110000) (excluding surrogates) whose category
    is Cc, Cf, Zl, or Zp, the sanitizer removes it."""
    disallowed = {"Cc", "Cf", "Zl", "Zp"}
    bad_chars = [
        chr(cp)
        for cp in range(0x110000)
        if not (0xD800 <= cp <= 0xDFFF) and unicodedata.category(chr(cp)) in disallowed
    ]
    assert len(bad_chars) > 0

    for ch in bad_chars:
        assert _clean_field(ch) == ""
        assert _clean_field(f"a{ch}b") == "ab"

    all_bad = "".join(bad_chars)
    assert _clean_field(all_bad) == ""
    assert _clean_field(f"start_{all_bad}_end") == "start__end"

    # Verify end-to-end in pick_with_model prompt generation for target and candidates
    cands = [Element("@e1", f"btn{all_bad}", f"Save{all_bad}", context=f"Ctx{all_bad}")]
    record = []
    ollama = _make_ollama_pick(cfg_resolve, ref_reply="@e1", record=record)
    pick_with_model(f"Target{all_bad}", cands, ollama, cfg_resolve)
    user_content = next(
        m["content"] for m in record[0]["messages"] if m["role"] == "user"
    )
    lines = user_content.splitlines()
    target_parsed = json.loads(lines[0])
    cand_parsed = json.loads(lines[1])
    assert target_parsed == "Target"
    assert cand_parsed == {"ref": "@e1", "role": "btn", "name": "Save", "ctx": "Ctx"}
    for ch in bad_chars:
        assert ch not in target_parsed
        for val in cand_parsed.values():
            assert ch not in val



# ===========================================================================
# Timeout logic: warm vs cold model — assert actual timeout values
# ===========================================================================

def test_timeout_warm_model_value(cfg_resolve):
    """When model is warm, timeout sent to httpx must equal timeout_base."""
    timeouts_seen = []

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if req.url.path == "/api/chat":
            # We can't directly read the httpx client timeout from the request,
            # but we can verify the payload was received (call happened).
            body = json.loads(req.content)
            timeouts_seen.append(body)
            return httpx.Response(200, json={
                "message": {"content": json.dumps({"ref": "@e1"})},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(404)

    # Instrument _client to capture the timeout argument
    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    captured_timeouts = []
    original_client = ollama._client

    def capturing_client(timeout):
        captured_timeouts.append(timeout)
        return original_client(timeout)

    ollama._client = capturing_client

    cands = _make_elements("Save", "Close")
    pick_with_model("Save", cands, ollama, cfg_resolve)

    # Warm model: timeout = timeout_base = 10.0
    chat_timeouts = captured_timeouts[1:]  # skip /api/ps timeout
    assert len(chat_timeouts) >= 1
    assert chat_timeouts[-1] == pytest.approx(cfg_resolve.timeout_base)


def test_timeout_cold_model_value(tmp_path):
    """When model is cold, timeout = timeout_base + cold_load_extra."""
    cfg = Config(
        ollama_url="http://ollama.test:11435",
        model="gpt-oss:20b",
        sessions_dir=str(tmp_path / ".sessions"),
        resolve_with_model=True,
        timeout_base=10.0,
        cold_load_extra=45.0,
    )

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            # Report no loaded models → cold
            return httpx.Response(200, json={"models": []})
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={
                "message": {"content": json.dumps({"ref": "@e1"})},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(404)

    ollama = OllamaClient(cfg, transport=httpx.MockTransport(handler))
    captured_timeouts = []
    original_client = ollama._client

    def capturing_client(timeout):
        captured_timeouts.append(timeout)
        return original_client(timeout)

    ollama._client = capturing_client

    cands = _make_elements("Save", "Close")
    pick_with_model("Save", cands, ollama, cfg)

    # Cold model: timeout = timeout_base + cold_load_extra = 55.0
    expected = cfg.timeout_base + cfg.cold_load_extra
    chat_timeouts = captured_timeouts[1:]  # skip /api/ps timeout
    assert len(chat_timeouts) >= 1
    assert chat_timeouts[-1] == pytest.approx(expected)


# ===========================================================================
# chat_json backward-compatibility tests
# (existing behaviour must remain unchanged)
# ===========================================================================

def _make_ollama_json(cfg, content, record=None):
    """OllamaClient that returns *content* (str) for /api/chat."""

    def handler(req: httpx.Request):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": cfg.model}]})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": cfg.model}]})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if record is not None:
                record.append(body)
            return httpx.Response(200, json={
                "message": {"content": content},
                "eval_count": 60,
                "eval_duration": 5_000_000_000,
                "total_duration": 6_000_000_000,
            })
        return httpx.Response(404)

    return OllamaClient(cfg, transport=httpx.MockTransport(handler))


def test_chat_json_happy_path(cfg_resolve):
    payload = {"status": "finished", "summary": "done", "confidence": 0.9}
    ollama = _make_ollama_json(cfg_resolve, json.dumps(payload))
    answer, stats = ollama.chat_json("sys", "usr", timeout=10.0)
    assert answer["summary"] == "done"
    assert stats["eval_tokens"] == 60
    assert stats["tokens_per_second"] == pytest.approx(12.0)


def test_chat_json_uses_answer_schema(cfg_resolve):
    """chat_json must send ANSWER_SCHEMA as format, not the pick schema."""
    record = []
    payload = {"status": "finished", "summary": "ok", "confidence": 1.0}
    ollama = _make_ollama_json(cfg_resolve, json.dumps(payload), record=record)
    ollama.chat_json("sys", "usr", timeout=10.0)
    assert record[0]["format"] == ANSWER_SCHEMA


def test_chat_json_missing_summary_raises(cfg_resolve):
    payload = {"status": "finished"}  # no "summary"
    ollama = _make_ollama_json(cfg_resolve, json.dumps(payload))
    with pytest.raises(CuError) as exc_info:
        ollama.chat_json("sys", "usr", timeout=10.0)
    assert exc_info.value.code == MODEL_FAILED
    assert "summary" in str(exc_info.value)


def test_chat_json_non_json_raises(cfg_resolve):
    ollama = _make_ollama_json(cfg_resolve, "not json")
    with pytest.raises(CuError) as exc_info:
        ollama.chat_json("sys", "usr", timeout=10.0)
    assert exc_info.value.code == MODEL_FAILED


def test_chat_json_think_gpt_oss(cfg_resolve):
    """chat_json must still send think='low' for gpt-oss models."""
    record = []
    payload = {"status": "finished", "summary": "ok", "confidence": 0.9}
    ollama = _make_ollama_json(cfg_resolve, json.dumps(payload), record=record)
    ollama.chat_json("sys", "usr", timeout=10.0)
    assert record[0]["think"] == "low"


def test_chat_json_timeout_error(cfg_resolve):
    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            raise httpx.ReadTimeout("timeout", request=req)
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    with pytest.raises(CuError) as exc_info:
        ollama.chat_json("sys", "usr", timeout=10.0)
    assert exc_info.value.code == MODEL_TIMEOUT


def test_chat_json_unreachable_error(cfg_resolve):
    def handler(req: httpx.Request):
        raise httpx.ConnectError("refused", request=req)

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    with pytest.raises(CuError) as exc_info:
        ollama.chat_json("sys", "usr", timeout=10.0)
    assert exc_info.value.code == OLLAMA_UNREACHABLE


def test_chat_json_missing_summary_raises_before_stats_malformed(cfg_resolve):
    """summary check in chat_json fires even when stats fields are malformed.

    Verifies the original ordering: answer-validation happens before stats,
    so a missing 'summary' always raises MODEL_FAILED regardless of stats.
    """

    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            # Valid JSON but no summary; stats are also bad (string eval_count)
            payload = {"status": "finished"}  # no summary
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": "bad",         # malformed — but should never be reached
                "eval_duration": 1_000_000_000,
                "total_duration": 2_000_000_000,
            })
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    with pytest.raises(CuError) as exc_info:
        ollama.chat_json("sys", "usr", timeout=10.0)
    # Must be MODEL_FAILED from summary check, not from stats TypeError
    assert exc_info.value.code == MODEL_FAILED
    assert "summary" in str(exc_info.value).lower()


# ===========================================================================
# chat_schema direct tests
# ===========================================================================

def test_chat_schema_returns_parsed_dict_and_stats(cfg_resolve):
    custom_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    payload = {"x": 42}

    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": 20, "eval_duration": 2_000_000_000, "total_duration": 3_000_000_000,
            })
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    answer, stats = ollama.chat_schema("sys", "usr", custom_schema, timeout=10.0)
    assert answer == {"x": 42}
    assert stats["eval_tokens"] == 20


def test_chat_schema_sends_correct_format(cfg_resolve):
    custom_schema = {"type": "object", "properties": {"y": {"type": "string"}}}
    record = []
    payload = {"y": "hello"}

    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            record.append(body)
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    ollama.chat_schema("sys", "usr", custom_schema, timeout=10.0)
    assert record[0]["format"] == custom_schema


def test_chat_schema_num_predict_sent(cfg_resolve):
    record = []
    payload = {"ref": None}

    def handler(req: httpx.Request):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            record.append(body)
            return httpx.Response(200, json={
                "message": {"content": json.dumps(payload)},
                "eval_count": 5, "eval_duration": 500_000_000, "total_duration": 600_000_000,
            })
        return httpx.Response(200, json={"models": []})

    ollama = OllamaClient(cfg_resolve, transport=httpx.MockTransport(handler))
    ollama.chat_schema("sys", "usr", _REF_PICK_SCHEMA, timeout=10.0, num_predict=128)
    assert record[0]["options"]["num_predict"] == 128
