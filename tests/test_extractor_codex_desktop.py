"""Real home fixtures and explicitly synthetic Codex thread-view regressions."""
from pathlib import Path

import pytest

from mcp_computer_use.extractors import extract, select_profile, wants_snapshot
from mcp_computer_use.extractors.codex_desktop import extract_codex_desktop, codex_snapshot_details
from conftest import CLAUDE_TEXT


FIXTURES = Path(__file__).parent / "fixtures" / "codex_desktop"


def fixture(state, kind):
    return (FIXTURES / f"{state}_{kind}.txt").read_text(encoding="utf-8")


def read_state(state, source="combined", **kwargs):
    text = fixture(state, "text") if source != "snapshot" else ""
    snapshot = fixture(state, "snapshot") if source != "text" else None
    return extract(text, "ChatGPT", snapshot=snapshot, **kwargs)


def test_real_home_text_identifies_active_composer_project():
    result = read_state("home", "text")
    assert result.extractor == "codex_desktop"
    assert result.status == "unknown"
    assert result.details["project"] == "project-alpha"
    assert "session" not in result.details and "mode" not in result.details
    assert "What should we work on" in result.last_message


def test_real_home_snapshot_identifies_project_and_mode():
    result = read_state("home", "snapshot")
    assert result.status == "unknown"
    assert result.details["project"] == "project-alpha" and result.details["mode"] == "Codex"
    assert "session" not in result.details
    assert "waiting_for_user" not in result.details


def test_real_home_combined_does_not_select_sidebar_project_or_chat():
    result = read_state("home")
    assert result.details == {"project": "project-alpha", "mode": "Codex"}
    assert result.status == "unknown"
    assert "project-beta" not in result.last_message
    assert "project-gamma" not in result.last_message


@pytest.mark.parametrize("source", ["text", "snapshot", "combined"])
def test_synthetic_working_fixture(source):
    result = read_state("working", source)
    assert result.extractor == "codex_desktop"
    if source == "text":
        assert result.status == "unknown"
        assert "session" not in result.details and "project" not in result.details
        assert "Please refactor" in result.last_message  # Unclassified fallback, not an inferred assistant reply.
    else:
        assert result.status == "working"
        assert result.details["session"] == "Refactor parser"
        assert result.details["project"] == "project-alpha"
        assert "Please refactor" not in result.last_message
    assert "updating AST node visitors" in result.last_message
    assert "Stop" not in result.last_message


@pytest.mark.parametrize("source", ["text", "snapshot", "combined"])
def test_synthetic_finished_fixture(source):
    result = read_state("finished", source)
    assert result.status == ("unknown" if source == "text" else "finished")
    if source == "text":
        assert "session" not in result.details
    else:
        assert result.details["session"] == "Refactor parser"
    assert result.details["next_step"] == "Next step: run the integration tests."
    assert "Changed 3 files" in result.last_message
    assert "Good response" not in result.last_message


@pytest.mark.parametrize("source", ["text", "snapshot", "combined"])
def test_synthetic_waiting_fixture(source):
    result = read_state("waiting", source)
    if source == "text":
        assert result.status == "unknown"
        assert "waiting_for_user" not in result.details and "waiting_prompt" not in result.details
        assert "session" not in result.details
    else:
        assert result.status == "waiting"
        assert result.details["waiting_for_user"] is True
        assert result.details["waiting_prompt"] == "npm test"
        assert result.details["session"] == "Refactor parser"
    assert "need to run the test suite" in result.last_message


@pytest.mark.parametrize("source", ["text", "snapshot", "combined"])
def test_synthetic_failure_fixture(source):
    result = read_state("failure", source)
    assert result.status == ("unknown" if source == "text" else "finished")
    assert any("2 failed, 10 passed" in failure for failure in result.details["failures"])
    assert any("Error: build failed" in failure for failure in result.details["failures"])
    assert "inspect parser error" in result.details["next_step"]
    if source == "text":
        assert "session" not in result.details
    else:
        assert result.details["session"] == "Refactor parser"


@pytest.mark.parametrize("app", ["ChatGPT", "CHATGPT", "chatgpt", "Codex", "cOdEx", "Codex Desktop", "OpenAI.Codex"])
def test_codex_app_selection_and_snapshot_support(app):
    assert select_profile(app) == "codex_desktop"
    assert wants_snapshot(app)
    assert extract(fixture("working", "text"), app).status == "unknown"


@pytest.mark.parametrize("title", ["Codex", "Thread name - Codex", "CODEX workspace"])
def test_codex_title_selection(title):
    assert select_profile("Unknown App", window_title=title) == "codex_desktop"
    assert wants_snapshot("Unknown App", window_title=title)
    result = extract(fixture("finished", "text"), "Unknown App", window_title=title)
    assert result.extractor == "codex_desktop" and result.status == "unknown"


def test_codex_title_from_snapshot_routes_unknown_app():
    snapshot = fixture("working", "snapshot").replace('window "ChatGPT"', 'window "Codex"', 1)
    result = extract(fixture("working", "text"), "Unknown App", snapshot=snapshot)
    assert result.extractor == "codex_desktop" and result.status == "working"


def test_claude_selection_stays_first_even_with_codex_title():
    assert select_profile("Claude", "Codex") == "claude_code"
    result = extract(CLAUDE_TEXT, "Claude", window_title="Codex")
    assert result.extractor == "claude" and result.status == "finished"
    assert "916 UI tests" in result.last_message


def test_unrelated_apps_and_titles_remain_generic():
    assert select_profile("Notepad", "Notes") == "generic"
    assert not wants_snapshot("Notepad")
    result = extract("first\nfirst\nsecond", "Notepad", window_title="Notes")
    assert result.extractor == "generic" and result.status == "unknown"
    assert result.last_message == "first\nsecond"


@pytest.mark.parametrize("snapshot", [None, "", "not an accessibility tree", '[@e1] totally-unfamiliar "garbage"', "\x00\n\ufffd"])
def test_empty_text_and_garbage_snapshot_never_crash(snapshot):
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown" and result.last_message == ""
    assert result.details == {}


def test_unrecognized_codex_text_retains_generic_fallback_content():
    result = extract("hello\nhello\nunknown current view", "ChatGPT", snapshot="garbage")
    assert result.status == "unknown" and result.last_message == "hello\nunknown current view"


@pytest.mark.parametrize("word", ["Thinking", "Working", "Stop", "Approve", "Deny", "Copy"])
def test_unscoped_words_are_not_state_evidence(word):
    assert extract(word, "ChatGPT").status == "unknown"


def test_assistant_prose_without_completion_controls_stays_unknown():
    text = fixture("finished", "text").replace("Copy\nGood response\nBad response\n", "")
    result = extract(text, "ChatGPT")
    assert result.status == "unknown" and "Changed 3 files" in result.last_message
    assert result.details["next_step"]


def test_home_sidebar_stop_or_approval_does_not_become_current_state():
    snapshot = fixture("home", "snapshot").replace('button "Archive chat"', 'button "Stop"')
    snapshot += '\n  group "Approval prompt"\n    statictext "Allow command?"\n    statictext "npm test"\n    [@e100] button "Approve"\n'
    result = extract(fixture("home", "text"), "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown"
    assert "waiting_for_user" not in result.details


def test_latest_snapshot_assistant_turn_wins_over_earlier_stop():
    snapshot = fixture("finished", "snapshot").replace('group "Assistant turn"',
        'group "Assistant turn"\n                          statictext "Thinking…"\n                          [@e100] button "Stop"\n                        group "Assistant turn"', 1)
    assert extract("", "ChatGPT", snapshot=snapshot).status == "finished"


def test_approval_requires_actual_approval_controls():
    snapshot = fixture("waiting", "snapshot").replace('button "Approve"', 'statictext "Approve"').replace('button "Deny"', 'statictext "Deny"')
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown" and "waiting_for_user" not in result.details


def test_text_conflicting_controls_are_unknown():
    text = fixture("waiting", "text").replace("I refactored", "Assistant:\nI refactored", 1)
    text = text.replace("Approve\nDeny", "Stop\nApprove\nDeny")
    result = extract(text, "ChatGPT")
    assert result.status == "unknown" and "waiting_for_user" not in result.details


@pytest.mark.parametrize("limit", [0, 1, 2, 40, 100])
def test_codex_message_is_bounded(limit):
    result = read_state("finished", max_chars=limit)
    assert len(result.last_message) <= limit
    assert result.truncated


def test_failure_detection_preserves_shared_zero_failure_semantics():
    text = fixture("failure", "text").replace("Test suite completed with failures: 2 failed, 10 passed", "All tests pass, 0 failed")
    text = text.replace("Error: build failed while compiling src/visitors.ts", "no errors found")
    text = text.replace("inspect parser error", "inspect parser types")
    result = extract(text, "ChatGPT")
    assert "failures" not in result.details


def test_snapshot_details_are_available_to_registry_parser():
    details = codex_snapshot_details(fixture("waiting", "snapshot"))
    assert details["waiting_prompt"] == "npm test" and details["mode"] == "Codex"
    assert details["folder"] == "C:\\work\\project-alpha" and details["branch"] == "main"


def test_direct_profile_supports_empty_input():
    result = extract_codex_desktop([], "Codex", snapshot="garbage")
    assert result.status == "unknown" and result.last_message == ""
    assert result.to_dict()["extractor"] == "codex_desktop"



def test_current_thread_header_overrides_stale_composer_project():
    snapshot = fixture("working", "snapshot").replace('button "New chat"', 'button "Change project: stale-project"', 1)
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.details["project"] == "project-alpha"


def test_new_user_turn_does_not_inherit_old_finished_state():
    snapshot = fixture("finished", "snapshot").replace('                      group "Composer utility bar"',
        '                        group "User turn"\n                          statictext "You said:"\n                          statictext "Another task"\n                      group "Composer utility bar"')
    result = extract(fixture("finished", "text"), "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown"


def test_later_completion_actions_supersede_earlier_stop_button():
    snapshot = fixture("finished", "snapshot").replace('group "Turn actions"', '[@e100] button "Stop"\n                          group "Turn actions"')
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "finished"


@pytest.mark.parametrize("snapshot", [0, 123, {}, []])
def test_nontext_snapshot_is_ignored_conservatively(snapshot):
    result = extract(fixture("working", "text"), "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown"



@pytest.mark.parametrize("word", ["Stop", "Approve", "Deny", "Thinking…", "Working…", "Copy"])
def test_snapshot_message_control_words_do_not_override_finished(word):
    snapshot = fixture("finished", "snapshot").replace('statictext "Changed 3 files:', f'statictext "{word}"\n                          statictext "Changed 3 files:', 1)
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "finished"
    assert word in result.last_message
    assert "waiting_for_user" not in result.details


def text_thread(body, controls="Copy\nGood response\nBad response", user="Please make a change"):
    return f"Thread: Refactor parser\nWorkspace: project-alpha\nYou said:\n{user}\nAssistant:\n{body}\n{controls}\nMessage ChatGPT\nSend"


@pytest.mark.parametrize("word", ["Stop", "Approve", "Deny", "Thinking…", "Working…"])
def test_text_message_control_words_are_content_before_trailing_controls(word):
    result = extract(text_thread(f"Here is an example:\n{word}\nEnd of example."), "ChatGPT")
    assert result.status == "finished"
    assert word in result.last_message
    assert "waiting_for_user" not in result.details


def test_text_finished_controls_after_last_stop_win():
    result = extract(text_thread("Complete.", "Stop\nCopy\nGood response\nBad response"), "ChatGPT")
    assert result.status == "finished"


def test_text_stop_after_finished_controls_is_conflicting():
    result = extract(text_thread("Complete.", "Copy\nGood response\nBad response\nStop"), "ChatGPT")
    assert result.status == "unknown"


def test_text_control_words_without_composer_are_not_controls():
    text = text_thread("Complete.").replace("Message ChatGPT\nSend", "")
    assert extract(text, "ChatGPT").status == "unknown"


def test_snapshot_statictext_approval_controls_cannot_create_waiting():
    snapshot = fixture("waiting", "snapshot").replace('button "Approve"', 'statictext "Approve"')
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"


@pytest.mark.parametrize("controls", ["Approve", "Deny", "Allow", "Approve\nAllow"])
def test_text_approval_requires_paired_trailing_controls(controls):
    result = extract(text_thread("Allow command?\nnpm test", controls), "ChatGPT")
    assert result.status == "unknown" and "waiting_for_user" not in result.details


@pytest.mark.parametrize("controls", ["Approve\nDeny", "Allow\nDeny"])
def test_text_approval_pair_and_prompt_can_report_waiting(controls):
    result = extract(text_thread("Allow command?\nnpm test", controls), "ChatGPT")
    assert result.status == "waiting"
    assert result.details["waiting_prompt"] == "npm test"


def test_text_prose_mentioning_approval_does_not_report_waiting():
    result = extract(text_thread("Allow command?\nApprove\nDeny\nThese lines explain a dialog."), "ChatGPT")
    assert result.status == "finished" and "waiting_for_user" not in result.details


def test_text_multiline_user_is_delimited_by_assistant_marker():
    text = text_thread("Changed parser.", user="Please examine these words:\nStop\nThinking…\nApprove\nDeny\nThen make a change")
    result = extract(text, "ChatGPT")
    assert result.status == "finished" and result.last_message == "Changed parser."
    assert "Please examine" not in result.last_message


def test_text_multiline_user_with_unclear_boundary_stays_unknown():
    text = text_thread("Changed parser.", user="Please examine:\nStop\nThinking…\nApprove\nDeny").replace("Assistant:\n", "")
    result = extract(text, "ChatGPT")
    assert result.status == "unknown" and "Changed parser." in result.last_message
    assert "waiting_for_user" not in result.details


def test_snapshot_multiline_user_control_words_are_not_assistant_body():
    snapshot = fixture("finished", "snapshot").replace('statictext "Please refactor the parser module to split monolithic visitor functions."',
        'statictext "Please refactor"\n                          statictext "Stop"\n                          statictext "Thinking…"\n                          statictext "Approve"')
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "finished" and "Stop" not in result.last_message
    assert "Please refactor" not in result.last_message


@pytest.mark.parametrize("label, role, expected", [("Thinking…", "statictext", "unknown"),
                                                   ("Working…", "statictext", "unknown"),
                                                   ("Thinking…", "status", "working")])
def test_snapshot_activity_requires_status_role_outside_assistant_message(label, role, expected):
    snapshot = fixture("working", "snapshot").replace('[@e22] button "Stop"', '')
    snapshot = snapshot.replace('                        group "Assistant turn"',
        f'                        {role} "{label}"\n                        group "Assistant turn"', 1)
    assert extract("", "ChatGPT", snapshot=snapshot).status == expected


def test_snapshot_status_inside_message_is_not_working():
    snapshot = fixture("working", "snapshot").replace('[@e22] button "Stop"', '').replace('statictext "Thinking…"', 'status "Thinking…"')
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"


def test_snapshot_completion_following_external_status_is_finished():
    snapshot = fixture("finished", "snapshot").replace('                        group "Assistant turn"',
        '                        status "Thinking…"\n                        group "Assistant turn"', 1)
    assert extract("", "ChatGPT", snapshot=snapshot).status == "finished"


def test_snapshot_status_after_completion_is_conflicting():
    snapshot = fixture("finished", "snapshot").replace('                      group "Composer utility bar"',
        '                        status "Thinking…"\n                      group "Composer utility bar"', 1)
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"


def test_snapshot_statictext_copy_and_feedback_do_not_report_finished():
    snapshot = fixture("finished", "snapshot").replace('button "Copy"', 'statictext "Copy"').replace('button "Good response"', 'statictext "Good response"').replace('button "Bad response"', 'statictext "Bad response"')
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"


def test_text_reordered_labelled_header_is_not_positional():
    text = text_thread("Complete.")
    text = text.replace("Thread: Refactor parser\nWorkspace: project-alpha", "Branch: main\nDirectory: C:\\work\\project-alpha\nWorkspace: project-alpha\nThread: Refactor parser")
    result = extract(text, "ChatGPT")
    assert result.details["session"] == "Refactor parser"
    assert result.details["project"] == "project-alpha"
    assert result.details["folder"] == "C:\\work\\project-alpha"


def test_text_unlabelled_reordered_header_does_not_invent_session_or_project():
    text = text_thread("Complete.").replace("Thread: Refactor parser\nWorkspace: project-alpha", "project-alpha\nOther sidebar label\nRefactor parser\nDirectory: C:\\work\\project-alpha")
    result = extract(text, "ChatGPT")
    assert "session" not in result.details and "project" not in result.details
    assert result.details["folder"] == "C:\\work\\project-alpha"


@pytest.mark.parametrize("marker", ["Next step: run tests.", "Next steps: run tests, then build.", "השלב הבא: להריץ בדיקות."])
def test_explicit_next_step_markers(marker):
    assert extract(text_thread(marker), "ChatGPT").details["next_step"] == marker


@pytest.mark.parametrize("prose", ["Please run tests.", "You should run tests.", "Next, run tests.", "Restart the app."])
def test_actionable_prose_is_not_an_explicit_next_step(prose):
    assert "next_step" not in extract(text_thread(prose), "ChatGPT").details


def test_metadata_fields_are_bounded():
    value = "x" * 500
    text = text_thread("Complete.").replace("Thread: Refactor parser\nWorkspace: project-alpha", f"Thread: {value}\nWorkspace: {value}\nDirectory: {value}\nSwitch mode, current mode: {value}")
    details = extract(text, "ChatGPT").details
    for key in ("project", "session", "folder", "mode"):
        assert details[key] == value[:120]


def test_snapshot_metadata_fields_are_bounded():
    value = "x" * 500
    snapshot = fixture("finished", "snapshot").replace('heading "Refactor parser"', f'heading "{value}"')
    snapshot = snapshot.replace('Switch mode, current mode: Codex', f'Switch mode, current mode: {value}')
    snapshot = snapshot.replace('button "project-alpha"', f'button "Project: {value}"')
    snapshot = snapshot.replace('Directory: C:\\work\\project-alpha', f'Directory: {value}')
    details = extract("", "ChatGPT", snapshot=snapshot).details
    for key in ("project", "session", "folder", "mode"):
        assert details[key] == value[:120]


def test_package_reuses_snapshot_parse_for_codex(monkeypatch):
    from mcp_computer_use import extractors
    from mcp_computer_use.extractors import codex_desktop
    original = extractors.parse_snapshot
    calls = []
    def counted(snapshot):
        calls.append(snapshot)
        return original(snapshot)
    def unexpected(snapshot):
        raise AssertionError("snapshot nodes must be reused")
    monkeypatch.setattr(extractors, "parse_snapshot", counted)
    monkeypatch.setattr(codex_desktop, "parse_snapshot", unexpected)
    result = extract(fixture("finished", "text"), "ChatGPT", snapshot=fixture("finished", "snapshot"))
    assert result.status == "finished" and len(calls) == 1


def test_direct_profile_parses_snapshot_once(monkeypatch):
    from mcp_computer_use.extractors import codex_desktop
    original = codex_desktop.parse_snapshot
    calls = []
    def counted(snapshot):
        calls.append(snapshot)
        return original(snapshot)
    monkeypatch.setattr(codex_desktop, "parse_snapshot", counted)
    result = extract_codex_desktop([], "ChatGPT", snapshot=fixture("finished", "snapshot"))
    assert result.status == "finished" and len(calls) == 1



def test_status_inside_earlier_assistant_message_does_not_activate_current_turn():
    snapshot = fixture("working", "snapshot").replace('[@e22] button "Stop"', '')
    snapshot = snapshot.replace('                        group "Assistant turn"',
        '                        group "Assistant turn"\n                          status "Thinking…"\n                        group "Assistant turn"', 1)
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"


def test_external_status_cannot_resolve_conflicting_snapshot_controls():
    snapshot = fixture("waiting", "snapshot").replace('                        group "Assistant turn"',
        '                        status "Thinking…"\n                        group "Assistant turn"', 1)
    snapshot = snapshot.replace('[@e22] button "Approve"', '[@e100] button "Stop"\n                            [@e22] button "Approve"')
    result = extract("", "ChatGPT", snapshot=snapshot)
    assert result.status == "unknown" and "waiting_for_user" not in result.details


def test_status_outside_conversation_and_composer_is_ignored():
    snapshot = fixture("working", "snapshot").replace('[@e22] button "Stop"', '')
    snapshot += '\n  status "Thinking…"\n'
    assert extract("", "ChatGPT", snapshot=snapshot).status == "unknown"
