"""Tests for safety module: blocklist, dangerous action checks, log auditing, and TTL purge."""
from __future__ import annotations

import json
import multiprocessing
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mcp_computer_use.config import Config, load_config
from mcp_computer_use.safety import (
    DEFAULT_BLOCKLIST,
    _LOG_LOCK,
    blocklist,
    is_dangerous,
    log_action,
    purge_log,
)


def _multiprocess_append_worker(sessions_dir: str, proc_id: int, count: int):
    cfg = Config(sessions_dir=sessions_dir)
    for i in range(count):
        log_action(
            cfg,
            action="click",
            client=f"proc_{proc_id}",
            target=f"target_{proc_id}_{i}",
        )


def test_default_blocklist_contains_required_en_and_he():
    """Verify DEFAULT_BLOCKLIST contains all base English and Hebrew dangerous words/forms."""
    assert isinstance(DEFAULT_BLOCKLIST, tuple)

    # Required English words
    for word in ("Delete", "Remove", "Send", "Allow", "Approve", "Accept", "Pay",
                 "Buy", "Purchase", "Submit", "Confirm", "Publish", "Merge",
                 "Uninstall", "Format"):
        assert word in DEFAULT_BLOCKLIST

    # Required Hebrew base words & forms & plural imperatives
    for word in ("מחק", "הסר", "שלח", "אשר", "אפשר", "שלם", "קנה", "פרסם",
                 "הסר התקנה", "מחיקה", "למחוק", "הסרה", "להסיר", "שליחה",
                 "לשלוח", "אישור", "לאשר", "לאפשר", "תשלום", "לשלם", "קנייה",
                 "לקנות", "פרסום", "לפרסם", "מחקו", "הסירו", "שלחו", "אשרו",
                 "אפשרו", "שלמו", "קנו", "פרסמו"):
        assert word in DEFAULT_BLOCKLIST


def test_en_and_he_blocked():
    """EN+HE blocked: 'Delete', 'Send message', 'Deleted', 'Submitting', 'שלח', 'ושלח', 'הסר התקנה', 'למחוק'."""
    assert is_dangerous("Delete", None, DEFAULT_BLOCKLIST) == "Delete"
    assert is_dangerous("Send message", None, DEFAULT_BLOCKLIST) == "Send"
    assert is_dangerous("Deleted", None, DEFAULT_BLOCKLIST) == "Delete"
    assert is_dangerous("Submitting", None, DEFAULT_BLOCKLIST) == "Submit"
    assert is_dangerous("שלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("ושלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("הסר התקנה", None, DEFAULT_BLOCKLIST) == "הסר התקנה"
    assert is_dangerous("למחוק", None, DEFAULT_BLOCKLIST) in ("למחוק", "מחק")


def test_hebrew_plural_imperatives_blocked():
    """Hebrew plural imperatives: מחקו and ושלחו are blocked."""
    assert is_dangerous("מחקו", None, DEFAULT_BLOCKLIST) == "מחקו"
    assert is_dangerous("ושלחו", None, DEFAULT_BLOCKLIST) == "שלחו"
    assert is_dangerous("הסירו", None, DEFAULT_BLOCKLIST) == "הסירו"


def test_unicode_format_characters_stripped():
    """Category Cf format characters (U+2060, U+200B, U+FEFF) are stripped before matching."""
    sneaky_delete = "De\u2060le\u200bte\ufeff"
    assert is_dangerous(sneaky_delete, None, DEFAULT_BLOCKLIST) == "Delete"


def test_element_name_trigger():
    """Element name triggers block even if target is harmless ('OK' target + 'Delete file' element)."""
    assert is_dangerous("OK", "Delete file", DEFAULT_BLOCKLIST) == "Delete"
    assert is_dangerous("Next", "הסר התקנה", DEFAULT_BLOCKLIST) == "הסר התקנה"
    assert is_dangerous("Submit button", "Save", DEFAULT_BLOCKLIST) == "Submit"


def test_allowed_words():
    """Allowed words should not be blocked: 'Seven', 'Formula', 'שבע', 'Close'."""
    for word in ("Seven", "Formula", "שבע", "Close"):
        assert is_dangerous(word, None, DEFAULT_BLOCKLIST) is None
        assert is_dangerous("Cancel", word, DEFAULT_BLOCKLIST) is None


def test_latin_prefix_rules():
    """Latin prefix rule: starts with entry and <= 4 extra chars."""
    assert is_dangerous("Sender", None, DEFAULT_BLOCKLIST) == "Send"
    assert is_dangerous("Sending", None, DEFAULT_BLOCKLIST) == "Send"
    assert is_dangerous("Senders", None, DEFAULT_BLOCKLIST) == "Send"
    assert is_dangerous("Formatting", None, DEFAULT_BLOCKLIST) == "Format"

    assert is_dangerous("Sendingss", None, DEFAULT_BLOCKLIST) is None


def test_hebrew_prefix_rules():
    """Hebrew single prefix letter from והבלמשכ."""
    assert is_dangerous("ושלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("השלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("בשלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("לשלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("משלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("ששלח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("כשלח", None, DEFAULT_BLOCKLIST) == "שלח"

    assert is_dangerous("ומהסר", None, DEFAULT_BLOCKLIST) is None
    assert is_dangerous("אשלח", None, DEFAULT_BLOCKLIST) is None

    assert is_dangerous("שָׁלַח", None, DEFAULT_BLOCKLIST) == "שלח"
    assert is_dangerous("מְחַק", None, DEFAULT_BLOCKLIST) == "מחק"


def test_custom_toml_blocklist_adds_while_delete_still_blocked(tmp_path: Path):
    """Custom TOML [safety] blocklist adds entries while 'Delete' is still blocked."""
    toml_content = """
[safety]
blocklist = ["Archive"]
"""
    cfg_file = tmp_path / "mcp-computer-use.toml"
    cfg_file.write_text(toml_content, encoding="utf-8")

    cfg = load_config(cfg_file)
    bl = blocklist(cfg)

    assert "Archive" in bl
    assert "Delete" in bl
    assert is_dangerous("Archive", None, bl) == "Archive"
    assert is_dangerous("Delete", None, bl) == "Delete"


def test_toml_only_loads_blocklist(tmp_path: Path):
    """TOML-only test (env={}) proving [safety] blocklist loads correctly."""
    toml_content = """
[safety]
blocklist = ["CustomOnly"]
"""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_content, encoding="utf-8")

    cfg = load_config(cfg_file, env={})
    bl = blocklist(cfg)

    assert "CustomOnly" in bl
    assert "Delete" in bl
    assert cfg.safety_blocklist == "CustomOnly"


def test_toml_blocklist_extra_and_env(tmp_path: Path, monkeypatch):
    """blocklist_extra in TOML and MCP_CU_SAFETY_BLOCKLIST_EXTRA environment variable."""
    toml_content = """
[mcp_computer_use.safety]
blocklist_extra = ["DropTable"]
"""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_content, encoding="utf-8")

    monkeypatch.setenv("MCP_CU_SAFETY_BLOCKLIST_EXTRA", "PurgeAll")
    cfg = load_config(cfg_file)
    bl = blocklist(cfg)

    assert "PurgeAll" in bl
    assert "Delete" in bl
    assert is_dangerous("PurgeAll", None, bl) == "PurgeAll"


def test_empty_list_returns_defaults(tmp_path: Path):
    """Empty list in config leaves defaults intact (cannot turn safety off)."""
    toml_content = """
[safety]
blocklist = []
blocklist_extra = []
"""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_content, encoding="utf-8")

    cfg = load_config(cfg_file)
    bl = blocklist(cfg)

    assert bl == list(dict.fromkeys(DEFAULT_BLOCKLIST))
    assert is_dangerous("Delete", None, bl) == "Delete"


def test_metadata_leakage_scrubbing(tmp_path: Path):
    """Any logged string field containing text S or >= 6-char fragments of S is replaced by <redacted>."""
    cfg = Config(sessions_dir=str(tmp_path))
    s = "SECRET_TOKEN_xyz_123"

    log_action(
        cfg,
        action="type",
        text=s,
        name=s,
        hint=s,
        target="x" + s,
        client="client_" + s[:8],
        session_id="session_" + s[2:10],
        error_code=s,
        app="app_" + s,
        ref=s[:7],
        role=s[:6],
    )

    log_file = tmp_path / "actions.log"
    line = log_file.read_text(encoding="utf-8").strip()
    record = json.loads(line)

    assert record["target"] == "<redacted>"
    assert record["name"] == "<redacted>"
    assert record["hint"] == "<redacted>"
    assert record["client"] == "<redacted>"
    assert record["session_id"] == "<redacted>"
    assert record["error_code"] == "<redacted>"
    assert record["app"] == "<redacted>"
    assert record["ref"] == "<redacted>"
    assert record["role"] == "<redacted>"
    assert record["text_len"] == len(s)
    assert "text" not in record

    # Assert no fragment of >= 6 consecutive chars of S appears in the file
    for i in range(len(s) - 5):
        frag = s[i : i + 6]
        assert frag not in line


def test_serialized_form_scrubbing(tmp_path: Path):
    """Scrubbing tests each field's serialized form: text='\\nSECRET', name='\\nSECR' -> name redacted."""
    cfg = Config(sessions_dir=str(tmp_path))
    text = "\nSECRET"
    name = "\nSECR"

    log_action(cfg, action="type", text=text, name=name, target="harmless")

    log_file = tmp_path / "actions.log"
    file_bytes = log_file.read_bytes()
    line_str = file_bytes.decode("utf-8").strip()
    record = json.loads(line_str)

    # Name is redacted because serialized form of name contains 6-char fragment of serialized text
    assert record["name"] == "<redacted>"
    assert record["text_len"] == len(text)
    assert "text" not in record

    # Assert on final file bytes that no >= 6-char fragment of any form of text appears
    text_forms = [
        text,
        json.dumps(text, ensure_ascii=False)[1:-1],
        json.dumps(text, ensure_ascii=True)[1:-1],
    ]
    for form in text_forms:
        for i in range(len(form) - 5):
            frag = form[i : i + 6].encode("utf-8")
            assert frag not in file_bytes


def test_field_coverage_and_scrubbing(tmp_path: Path):
    """Field coverage: time is generated internally, action/result validated to 'other', confirmed is bool, all string fields scrubbed."""
    cfg = Config(sessions_dir=str(tmp_path))
    s = "SCRUB_TARGET_TOKEN"

    # 1. Invalid action and result become 'other', caller time is ignored, confirmed is bool
    log_action(
        cfg,
        action="invalid_act",
        result="invalid_res",
        time="2000-01-01T00:00:00Z",
        confirmed=1,
        target="valid_target",
    )

    log_file = tmp_path / "actions.log"
    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    rec1 = json.loads(lines[0])

    assert rec1["action"] == "other"
    assert rec1["result"] == "other"
    assert not rec1["time"].startswith("2000-01-01")
    assert rec1["confirmed"] is True

    # 2. Valid actions and results are preserved
    for act in ("click", "type"):
        for res in ("done", "blocked", "ambiguous", "not_found", "error"):
            log_action(cfg, action=act, result=res)

    # 3. All remaining string fields are scrubbed
    log_action(
        cfg,
        action="type",
        text=s,
        client="client_" + s,
        session_id="session_" + s,
        app="app_" + s,
        target="target_" + s,
        ref="ref_" + s,
        name="name_" + s,
        role="role_" + s,
        error_code="err_" + s,
        hint="hint_" + s,
    )

    all_lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    scrubbed_rec = json.loads(all_lines[-1])

    for field in ("client", "session_id", "app", "target", "ref", "name", "role", "error_code", "hint"):
        assert scrubbed_rec[field] == "<redacted>"


def test_thread_lock_timeout_returns_hint_and_appends_nothing(tmp_path: Path):
    """Holding the module thread lock causes log_action to timeout within ~5.5s and append nothing."""
    cfg = Config(sessions_dir=str(tmp_path))
    log_file = tmp_path / "actions.log"

    def holder(barrier: threading.Barrier, stop_evt: threading.Event):
        with _LOG_LOCK:
            barrier.wait()
            stop_evt.wait(timeout=10.0)

    barrier = threading.Barrier(2)
    stop_evt = threading.Event()
    t = threading.Thread(target=holder, args=(barrier, stop_evt))
    t.start()
    try:
        barrier.wait()
        start = time.monotonic()
        hint = log_action(cfg, action="click", target="button")
        elapsed = time.monotonic() - start

        assert hint == "action log skipped: lock timeout"
        assert 4.5 <= elapsed <= 6.5
        assert not log_file.exists()
    finally:
        stop_evt.set()
        t.join(timeout=5.0)


def test_log_action_whitelisted_fields_only_and_no_sentinel(tmp_path: Path):
    """Log line accepts only whitelisted fields and never logs raw or JSON-escaped sentinels."""
    cfg = Config(sessions_dir=str(tmp_path))

    sentinels = [
        "SENTINEL-7f3a",
        'SENTINEL"with_quotes"and\\slashes',
        "line1\nSENTINEL-B",
        "סודי-SENTINEL",
    ]

    for sentinel in sentinels:
        log_action(
            cfg,
            client="client_1",
            session_id="session_abc",
            app="Notepad",
            action="type",
            target="Text editor",
            ref="@e4",
            name="A" * 120,
            role="document",
            result="done",
            confirmed=False,
            error_code=None,
            text=sentinel,
            hint="some_hint",
            val="unwanted_val",
            unexpected_field="bad",
        )

    log_file = tmp_path / "actions.log"
    assert log_file.is_file()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4

    whitelisted_fields = {
        "time",
        "client",
        "session_id",
        "app",
        "action",
        "target",
        "ref",
        "name",
        "role",
        "result",
        "confirmed",
        "error_code",
        "text_len",
        "hint",
    }

    for idx, line in enumerate(lines):
        record = json.loads(line)
        assert set(record.keys()) == whitelisted_fields
        assert "text" not in record
        assert "val" not in record
        assert "unexpected_field" not in record
        assert len(record["name"]) == 80
        assert record["text_len"] == len(sentinels[idx])

        sentinel_raw = sentinels[idx].replace("\n", "")
        sentinel_escaped = json.dumps(sentinels[idx])[1:-1]
        for s in (sentinel_raw, sentinel_escaped):
            for i in range(len(s) - 5):
                frag = s[i : i + 6]
                assert frag not in line


def test_log_action_text_len_validation_and_capping(tmp_path: Path):
    """text_len must be non-negative int; target capped at 100 chars."""
    cfg = Config(sessions_dir=str(tmp_path))

    # Negative text_len -> None
    log_action(cfg, action="type", text_len=-5, target="a" * 150)
    # Non-integer text_len -> None
    log_action(cfg, action="type", text_len="invalid", target="normal")

    log_file = tmp_path / "actions.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()

    rec1 = json.loads(lines[0])
    assert rec1["text_len"] is None
    assert len(rec1["target"]) == 100

    rec2 = json.loads(lines[1])
    assert rec2["text_len"] is None


def test_lock_timeout_skips_append_and_returns_hint(tmp_path: Path):
    """If OS lock cannot be acquired within 5s, do NOT append and return 'action log skipped: lock timeout'."""
    cfg = Config(sessions_dir=str(tmp_path))
    log_file = tmp_path / "actions.log"

    with patch("mcp_computer_use.safety._acquire_file_lock", return_value=False):
        hint = log_action(cfg, action="click", target="button")
        assert hint == "action log skipped: lock timeout"

    assert not log_file.exists()


def test_purge_failure_returns_exact_hint(tmp_path: Path):
    """When purge_log fails (e.g. os.replace raises PermissionError), return exact hint."""
    cfg = Config(sessions_dir=str(tmp_path))
    log_file = tmp_path / "actions.log"

    old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    log_file.write_text(json.dumps({"time": old_time, "action": "click"}) + "\n", encoding="utf-8")

    with patch("os.replace", side_effect=PermissionError("Locked by another process")):
        hint = log_action(cfg, action="click", target="new_button")
        assert hint == "action log purge failed: PermissionError"

    # Line is still appended even if purge failed
    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    assert len(lines) == 2


def test_os_replace_failure_returns_hint_no_exception(tmp_path: Path):
    """Replacement failure (os.replace raising OSError) returns exact hint without raising."""
    cfg = Config(sessions_dir=str(tmp_path))

    with patch("os.replace", side_effect=OSError("Permission denied")):
        log_file = tmp_path / "actions.log"
        old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        log_file.write_text(json.dumps({"time": old_time, "action": "click"}) + "\n", encoding="utf-8")

        hint = log_action(cfg, action="click", target="test")
        assert hint == "action log purge failed: OSError"


def test_ttl_clamping_no_overflow(tmp_path: Path):
    """Clamp TTL to (0, 87600] hours; 1e300 and bad values never overflow or raise."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    log_file.write_text(json.dumps({"time": t_1h, "action": "click"}) + "\n", encoding="utf-8")

    for bad_ttl in (1e300, "1e300", 87600.0, 100000.0, float("nan"), float("inf"), -5.0, 0, None, "bad"):
        dropped = purge_log(log_file, ttl_hours=bad_ttl, now=now)
        assert dropped == 0


def test_purge_log_handles_invalid_ttl(tmp_path: Path):
    """Invalid TTL values (nan, inf, -1, None, bad string) fallback to 24h safely."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    log_file.write_text(json.dumps({"time": t_1h, "action": "click"}) + "\n", encoding="utf-8")

    for bad_ttl in (float("nan"), float("inf"), float("-inf"), -5.0, 0, None, "bad"):
        dropped = purge_log(log_file, ttl_hours=bad_ttl, now=now)
        assert dropped == 0


def test_purge_log_ttl_drops_25h_keeps_1h_and_drops_unparsable(tmp_path: Path):
    """TTL purge drops a 25 h line and keeps a 1 h line, and drops unparsable lines."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_25h = (now - timedelta(hours=25)).isoformat()
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    lines = [
        json.dumps({"time": t_25h, "action": "click", "target": "old"}),
        "not-valid-json-string",
        json.dumps({"time": t_1h, "action": "type", "target": "recent", "text_len": 5}),
        json.dumps({"no_time_field": True}),
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dropped = purge_log(log_file, ttl_hours=24.0, now=now)
    assert dropped == 3

    remaining = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    assert len(remaining) == 1
    kept_record = json.loads(remaining[0])
    assert kept_record["target"] == "recent"
    assert kept_record["time"] == t_1h


def test_purge_log_nothing_dropped_does_not_rewrite(tmp_path: Path):
    """When nothing is dropped, purge_log returns 0 and does not modify the file."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    content = json.dumps({"time": t_1h, "action": "click"}) + "\n"
    log_file.write_text(content, encoding="utf-8")

    with patch("os.replace") as mock_replace:
        dropped = purge_log(log_file, ttl_hours=24.0, now=now)
        assert dropped == 0
        mock_replace.assert_not_called()


def test_concurrent_writers_no_lost_updates(tmp_path: Path):
    """Threads appending concurrently while purging lose no lines."""
    cfg = Config(sessions_dir=str(tmp_path))
    total_threads = 8
    appends_per_thread = 20

    def worker(worker_id: int):
        for i in range(appends_per_thread):
            log_action(
                cfg,
                action="click",
                client=f"client_{worker_id}",
                target=f"target_{worker_id}_{i}",
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(total_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_file = tmp_path / "actions.log"
    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    assert len(lines) == total_threads * appends_per_thread


def test_concurrency_with_expired_records(tmp_path: Path):
    """Concurrency test with expired records: purge rewrites while threads append, no line lost."""
    cfg = Config(sessions_dir=str(tmp_path))
    log_file = tmp_path / "actions.log"

    old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    pre_lines = [json.dumps({"time": old_time, "action": "click", "target": f"old_{i}"}) for i in range(5)]
    log_file.write_text("\n".join(pre_lines) + "\n", encoding="utf-8")

    total_threads = 8
    appends_per_thread = 20

    def worker(worker_id: int):
        for i in range(appends_per_thread):
            log_action(
                cfg,
                action="click",
                client=f"thread_{worker_id}",
                target=f"target_{worker_id}_{i}",
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(total_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    records = [json.loads(l) for l in lines]
    targets = {r.get("target") for r in records}

    # All expired lines were purged
    for i in range(5):
        assert f"old_{i}" not in targets

    # All new lines from all threads are present
    assert len(lines) == total_threads * appends_per_thread
    for w in range(total_threads):
        for i in range(appends_per_thread):
            assert f"target_{w}_{i}" in targets


def test_two_processes_concurrent_append_with_purge(tmp_path: Path):
    """Two processes (multiprocessing spawn) appending concurrently with purging, all lines present."""
    log_file = tmp_path / "actions.log"

    old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    pre_lines = [json.dumps({"time": old_time, "action": "click", "target": f"old_{i}"}) for i in range(5)]
    pre_lines.append(json.dumps({"time": recent_time, "action": "click", "target": "keep_recent"}))
    log_file.write_text("\n".join(pre_lines) + "\n", encoding="utf-8")

    ctx = multiprocessing.get_context("spawn")
    count_per_proc = 15
    p1 = ctx.Process(target=_multiprocess_append_worker, args=(str(tmp_path), 1, count_per_proc))
    p2 = ctx.Process(target=_multiprocess_append_worker, args=(str(tmp_path), 2, count_per_proc))

    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    assert p1.exitcode == 0
    assert p2.exitcode == 0

    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    records = [json.loads(l) for l in lines]
    targets = {r.get("target") for r in records}

    assert "keep_recent" in targets
    for w in (1, 2):
        for i in range(count_per_proc):
            assert f"target_{w}_{i}" in targets


def test_jsonl_split_preserves_u2028_and_u2029(tmp_path: Path):
    """Lines containing U+2028 and U+2029 inside a field are not broken by purge_log."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    record = {
        "time": t_1h,
        "action": "click",
        "target": "line\u2028sep\u2029para",
    }
    log_file.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    dropped = purge_log(log_file, ttl_hours=24.0, now=now)
    assert dropped == 0

    content = log_file.read_text(encoding="utf-8")
    lines = [l for l in content.split("\n") if l.strip()]
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded["target"] == "line\u2028sep\u2029para"


def test_corrupt_non_utf8_log_handled_safely(tmp_path: Path):
    """Non-UTF-8 bytes in log file are handled with errors='replace' without crashing."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_1h = (now - timedelta(hours=1)).isoformat()

    log_file = tmp_path / "actions.log"
    valid_line = (json.dumps({"time": t_1h, "action": "click", "target": "ok"}) + "\n").encode("utf-8")
    corrupt_bytes = b"\xff\xfe\x80\x00\n" + valid_line
    log_file.write_bytes(corrupt_bytes)

    dropped = purge_log(log_file, ttl_hours=24.0, now=now)
    assert dropped == 1
    lines = [l for l in log_file.read_text(encoding="utf-8").split("\n") if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["target"] == "ok"
