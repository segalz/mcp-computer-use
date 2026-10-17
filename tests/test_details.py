"""Goal B: project / session / next step / failures extraction (from real Claude Code output)."""
import json
from pathlib import Path

from mcp_computer_use import core
from mcp_computer_use.extractors import (extract, claude_snapshot_details, find_failures,
                                         find_next_step, parse_snapshot)

from conftest import make_ollama

FIX = Path(__file__).parent / "fixtures"
TEXT = (FIX / "claude_code_text.txt").read_text(encoding="utf-8")
SNAP = (FIX / "claude_code_snapshot.txt").read_text(encoding="utf-8")


def test_text_only_details():
    d = extract(TEXT, "Claude").details
    assert d["project"] == "MyApp-Desktop"
    assert d["background_tasks"] == "1 running" and d["context_used"] == "93%"
    assert d["failures"] == ["Finished a background command (1 failed), ran 3 commands"]
    assert "אחרי ההתקנה אפשר לפתוח אותו מחדש" in d["next_step"]


def test_snapshot_details():
    d = claude_snapshot_details(SNAP)
    assert d["session"] == "Multimodal work in progress"
    assert d["project"] == "MyApp-Desktop"
    assert d["folder"] == "C:\\Develop" and d["branch"] == "fix/pre-api"
    assert d["model"] == "Opus 5.5 Medium"
    assert d["files"] == ["MyApp_0.1.0_x64-setup.exe"]
    assert "waiting_for_user" not in d


def test_combined_extract_keeps_text_and_adds_snapshot():
    ex = extract(TEXT, "Claude", snapshot=SNAP)
    assert ex.details["session"] == "Multimodal work in progress"
    assert ex.details["failures"] and ex.details["next_step"]
    assert "ddb86ca" in ex.last_message
    assert "Finished a background" not in ex.last_message  # tool lines still kept out of the text


def test_snapshot_label_with_inner_quotes():
    nodes = parse_snapshot('  statictext "now shows as "X Y" in full." id=_r_1_\n  [@e3] button "Allow" val="x"')
    assert nodes[0][2] == 'now shows as "X Y" in full.'
    assert nodes[1] == (2, "button", "Allow")
    assert claude_snapshot_details('  [@e3] button "Allow once"')["waiting_for_user"] is True


def test_failures_ignore_zero_failed():
    assert find_failures(["All 40 tests pass, 0 failed", "no errors found"]) == []
    assert find_failures(["Build failed: missing dep", "3 tests failing"]) == ["Build failed: missing dep",
                                                                                "3 tests failing"]


def test_next_step_english():
    body = ["Fixed it. All tests pass.", "Next step: run the installer and reopen the app.", "Thanks."]
    assert find_next_step(body) == "Next step: run the installer and reopen the app."
    assert find_next_step(["All done."]) == ""


def test_details_in_result_and_prompt(cfg, fake_cu):
    fake_cu.texts["Claude"] = TEXT
    fake_cu.snaps["Claude"] = SNAP
    sent = []
    res = core.look_at_screen("Which project and session are open?", "Claude", cfg, make_ollama(cfg, record=sent))
    assert res["details"]["project"] == "MyApp-Desktop"
    assert res["details"]["session"] == "Multimodal work in progress"
    user = sent[0]["messages"][1]["content"]
    assert "<details>" in user and "fix/pre-api" in user
    assert "changed_since_last" not in res  # stateless call
    assert ["snapshot", "-a", "Claude", "-i", "-c", "--human"] in fake_cu.calls


def test_snapshot_failure_is_not_fatal(cfg, fake_cu):
    fake_cu.texts["Claude"] = TEXT  # no snapshot registered -> agent-cu snapshot exits 1
    res = core.look_at_screen("Which project?", "Claude", cfg, make_ollama(cfg))
    assert "error" not in res and res["details"]["project"] == "MyApp-Desktop"


def test_snapshot_disabled(cfg, fake_cu):
    cfg.snapshot_details = False
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert not any(c[0] == "snapshot" for c in fake_cu.calls)
