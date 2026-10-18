"""Real anonymized Antigravity captures and synthetic VS Code panel fixtures."""
from pathlib import Path

import pytest

from mcp_computer_use import extractors
from mcp_computer_use.extractors import extract, select_profile, wants_snapshot
from mcp_computer_use.extractors.editor import (
    editor_snapshot_details, editor_title_details, extract_editor, is_editor_app, is_editor_title,
)


FIXTURES = Path(__file__).parent / "fixtures" / "editor"
TRUST = "Do you trust the authors of the files in this folder?"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def full():
    return extract("", "Antigravity IDE", snapshot=fixture("antigravity_trust_snapshot.txt"))


def interactive():
    return extract("", "Antigravity IDE", snapshot=fixture("antigravity_trust_interactive.txt"))


def problems():
    return extract("", "Code", snapshot=fixture("vscode_problems_snapshot.txt"))


def test_real_full_snapshot_metadata():
    result = full()
    assert result.extractor == "editor"
    assert result.details["project"] == "project-alpha"
    assert result.details["file"] == "parser.py"


def test_real_full_snapshot_waits_for_trust():
    result = full()
    assert result.status == "waiting"
    assert result.details["waiting_for_user"] is True
    assert result.details["waiting_prompt"] == TRUST
    assert "failures" not in result.details


def test_real_full_snapshot_summary_ignores_menus_and_loading_state():
    result = full()
    assert result.last_message == TRUST
    assert "Explorer" not in result.last_message and "File" not in result.last_message
    assert result.status != "working"


def test_real_interactive_snapshot_metadata():
    result = interactive()
    assert result.extractor == "editor" and result.details == {"project": "project-alpha", "file": "parser.py"}


def test_real_interactive_snapshot_does_not_invent_unexposed_prompt():
    result = interactive()
    assert result.status == "unknown" and "waiting_for_user" not in result.details
    assert "waiting_prompt" not in result.details and "failures" not in result.details


def test_real_interactive_snapshot_has_no_invented_content():
    assert interactive().last_message == ""


def test_real_text_metadata():
    result = extract(fixture("antigravity_trust_text.txt"), "Antigravity IDE")
    assert result.details["project"] == "project-alpha" and result.details["file"] == "parser.py"


def test_real_text_trust_prompt_has_priority_over_install_notification():
    result = extract(fixture("antigravity_trust_text.txt"), "Antigravity IDE")
    assert result.status == "waiting" and result.details["waiting_prompt"] == TRUST
    assert "failures" not in result.details


def test_real_text_agent_loading_is_not_a_running_task():
    text = fixture("antigravity_trust_text.txt")
    lines = [line for line in text.splitlines() if "?" not in line]
    result = extract("\n".join(lines), "Antigravity IDE")
    assert result.status == "unknown" and "waiting_for_user" not in result.details


def test_synthetic_problems_metadata():
    result = problems()
    assert result.details["project"] == "project-alpha" and result.details["file"] == "parser.py"
    assert result.extractor == "editor"


def test_synthetic_problems_are_errors_with_bounded_failure_evidence():
    result = problems()
    assert result.status == "error"
    assert any("2 errors, 1 warning" in line for line in result.details["failures"])
    assert any("parser.py:12 SyntaxError: invalid syntax" in line for line in result.details["failures"])
    assert any("FAILED tests/test_parser.py::test_unicode" in line for line in result.details["failures"])
    assert len(result.details["failures"]) <= 5
    assert all(len(line) <= 160 for line in result.details["failures"])


def test_synthetic_problems_next_step_is_explicit():
    result = problems()
    assert result.details["next_step"] == "Next step: fix parser.py:12 and rerun tests."
    assert "waiting_for_user" not in result.details
    assert "FAILED tests/test_parser.py::test_unicode" in result.last_message


def test_synthetic_clean_metadata():
    result = extract(fixture("vscode_clean_text.txt"), "Code")
    assert result.details["project"] == "project-alpha" and result.details["file"] == "parser.py"


def test_synthetic_clean_has_no_failures_or_prompts():
    result = extract(fixture("vscode_clean_text.txt"), "Code")
    assert result.status == "unknown"
    assert "failures" not in result.details and "waiting_for_user" not in result.details


def test_synthetic_clean_retains_terminal_pass_result_without_claiming_agent_finished():
    result = extract(fixture("vscode_clean_text.txt"), "Code")
    assert "3 passed" in result.last_message
    assert result.status not in {"working", "finished"}


@pytest.mark.parametrize("app", ["Code", "CODE", "Code - Insiders", "Cursor", "Antigravity IDE", "Windsurf", "VSCodium"])
def test_editor_app_selection(app):
    assert is_editor_app(app)
    assert select_profile(app) == "editor" and wants_snapshot(app)
    assert extract(fixture("vscode_clean_text.txt"), app).extractor == "editor"


@pytest.mark.parametrize("app, profile", [("Antigravity", "generic"), ("ChatGPT", "codex_desktop"),
                                         ("Codex", "codex_desktop"), ("Claude", "claude_code"),
                                         ("CodeHelper", "generic"), ("Notepad", "generic")])
def test_selection_does_not_capture_other_apps(app, profile):
    assert not is_editor_app(app)
    assert select_profile(app) == profile
    if app in {"Antigravity", "ChatGPT", "Codex", "Claude"}:
        assert select_profile(app, "parser.py - project-alpha - Visual Studio Code") != "editor"


@pytest.mark.parametrize("title", [
    "project-alpha - Antigravity IDE - parser.py",
    "parser.py - project-alpha - Visual Studio Code",
    "parser.py - project-alpha - Cursor",
    "parser.py - project-alpha - Antigravity IDE",
])
def test_supported_title_formats(title):
    assert is_editor_title(title)
    assert editor_title_details(title) == {"project": "project-alpha", "file": "parser.py"}
    assert select_profile("Unknown App", title) == "editor"
    assert wants_snapshot("Unknown App", title)
    result = extract("", "Unknown App", window_title=title)
    assert result.extractor == "editor" and result.details["file"] == "parser.py"


def test_snapshot_title_can_select_an_unknown_editor_app():
    result = extract("", "Unknown App", snapshot=fixture("antigravity_trust_interactive.txt"))
    assert result.extractor == "editor" and result.details["project"] == "project-alpha"


def test_registration_order_and_future_browser_slot():
    names = [name for name, _ in extractors.PROFILES]
    assert names[:3] == ["claude_code", "codex_desktop", "editor"]


@pytest.mark.parametrize("snapshot", [None, "", "garbage\nnot a tree", "\x00\n\ufffd", 42, {}])
def test_empty_or_garbage_structure_is_safe(snapshot):
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "unknown" and result.last_message == ""
    assert result.details == {}


def test_explorer_supplies_project_without_a_title():
    result = extract("Explorer\nExplorer Section: project-alpha\nTimeline", "Code")
    assert result.details["project"] == "project-alpha"


def test_single_editor_file_tab_can_supply_file_without_title():
    result = extract("", "Code", snapshot='group "Editor"\n  [@e1] tab "parser.py"')
    assert result.details["file"] == "parser.py"


def test_multiple_file_tabs_do_not_guess_the_active_file():
    result = extract("", "Code", snapshot='group "Editor"\n  [@e1] tab "parser.py"\n  [@e2] tab "tokens.py"')
    assert "file" not in result.details


def test_snapshot_source_text_failure_is_not_a_problems_or_terminal_signal():
    snapshot = 'group "Editor"\n  statictext "FAILED tests/test_parser.py::test_unicode"\n  statictext "2 errors"'
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "unknown" and "failures" not in result.details


def test_menu_and_activity_bar_error_words_do_not_become_failures():
    snapshot = 'menubar\n  [@e1] menuitem "Errors"\ngroup "Activity bar"\n  [@e2] tab "Problems"'
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "unknown" and "failures" not in result.details


def test_exception_diagnostic_without_count_is_still_an_error():
    result = extract("", "Code", snapshot='group "Problems"\n  listitem "parser.py:12 SyntaxError: invalid syntax"')
    assert result.status == "error" and "SyntaxError" in result.details["failures"][0]


@pytest.mark.parametrize("line", ["0 errors, 1 warning", "no errors found", "All tests pass, 0 failed", "3 passed"])
def test_shared_zero_failure_semantics(line):
    result = extract("Terminal\n" + line, "Code")
    assert result.status == "unknown" and "failures" not in result.details


def test_install_prompt_can_report_waiting_without_trust_dialog():
    snapshot = ('window "Install extension?"\n  statictext "Install extension?"\n'
                '  [@e1] button "Install"\n  [@e2] button "Cancel"')
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "waiting" and result.details["waiting_prompt"] == "Install extension?"


def test_question_in_source_text_without_dialog_is_not_a_waiting_prompt():
    snapshot = 'group "Editor"\n  statictext "Do you trust the authors of the files in this folder?"'
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "unknown" and "waiting_prompt" not in result.details


def test_modal_prompt_takes_priority_while_preserving_failures():
    snapshot = fixture("vscode_problems_snapshot.txt") + '\n  window "Install extension?"\n    [@e999] button "Install"\n'
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "waiting" and result.details["failures"]


@pytest.mark.parametrize("prose", ["Please run tests.", "You should restart.", "Next, run tests."])
def test_next_step_requires_an_explicit_marker(prose):
    assert "next_step" not in extract("Terminal\n" + prose, "Code").details


@pytest.mark.parametrize("marker", ["Next step: run tests.", "Next steps: build, then test.", "השלב הבא: להריץ בדיקות."])
def test_explicit_next_step_markers(marker):
    assert extract("Terminal\n" + marker, "Code").details["next_step"] == marker


@pytest.mark.parametrize("limit", [0, 1, 2, 40, 100])
def test_editor_summary_is_bounded(limit):
    result = extract("", "Code", max_chars=limit, snapshot=fixture("vscode_problems_snapshot.txt"))
    assert len(result.last_message) <= limit and result.truncated


def test_metadata_lists_and_waiting_prompt_bounds():
    huge = "x" * 500
    snapshot = f'window "{huge} - Antigravity IDE - {huge}"\n  dialog "Do you want to install {huge}?"\n    button "Install"\n  group "Problems"\n'
    snapshot += '\n'.join(f'    listitem "Error: issue {i} {huge}"' for i in range(10))
    result = extract("", "Code", snapshot=snapshot)
    assert all(len(result.details[key]) <= 200 for key in ("project", "file", "waiting_prompt"))
    assert len(result.details["failures"]) <= 5 and all(len(line) <= 160 for line in result.details["failures"])


def test_editor_snapshot_details_public_helper():
    details = editor_snapshot_details(fixture("antigravity_trust_snapshot.txt"))
    assert details["waiting_prompt"] == TRUST and details["project"] == "project-alpha"


def test_direct_editor_profile_supports_empty_input():
    result = extract_editor([], "Code", snapshot="garbage")
    assert result.extractor == "editor" and result.status == "unknown" and result.last_message == ""


def test_anonymized_fixtures_do_not_contain_user_directories():
    for path in FIXTURES.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        assert "\\users\\" not in text.lower() and "/users/" not in text.lower()
    assert "C:\\work\\project-alpha\\src\\parser.py" in fixture("antigravity_trust_snapshot.txt")
    assert "c:/work/" in fixture("antigravity_trust_text.txt")



def test_editor_title_suffix_outranks_codex_word_in_filename():
    title = "codex.py - project-alpha - Visual Studio Code"
    assert select_profile("Unknown App", title) == "editor"
    result = extract("", "Unknown App", window_title=title)
    assert result.extractor == "editor" and result.details["file"] == "codex.py"


def test_codex_and_claude_apps_still_outrank_editor_title():
    title = "parser.py - project-alpha - Visual Studio Code"
    assert select_profile("ChatGPT", title) == "codex_desktop"
    assert select_profile("Claude", title) == "claude_code"


def test_editor_profile_reuses_snapshot_nodes(monkeypatch):
    from mcp_computer_use.extractors import editor
    def unexpected(snapshot):
        raise AssertionError("editor must reuse the package's parsed nodes")
    monkeypatch.setattr(editor, "parse_snapshot", unexpected)
    result = extract("", "Code", snapshot=fixture("vscode_problems_snapshot.txt"))
    assert result.status == "error"


@pytest.mark.parametrize("source", ["# FAILED tests/a.py::t", "raise ValueError: bad"])
def test_text_menubar_terminal_does_not_open_output_panel(source):
    text = "File\nEdit\nSelection\nView\nGo\nRun\nTerminal\nHelp\n" + source
    result = extract(text, "Code")
    assert result.status == "unknown"
    assert "failures" not in result.details


@pytest.mark.parametrize("role", ["window", "dialog", "alert"])
def test_arbitrary_question_with_buttons_is_not_a_known_prompt(role):
    result = extract("", "Code", snapshot=f'{role} "Continue?"\n  button "Yes"\n  button "No"')
    assert result.status == "unknown" and "waiting_prompt" not in result.details


@pytest.mark.parametrize("role, waiting", [("dialog", True), ("alertdialog", True), ("alert", True), ("window", False)])
def test_save_changes_requires_dialog_role_and_buttons(role, waiting):
    snapshot = f'{role} "Do you want to save the changes?"\n  button "Save"\n  button "Cancel"'
    result = extract("", "Code", snapshot=snapshot)
    assert (result.status == "waiting") is waiting
    assert ("waiting_prompt" in result.details) is waiting


def test_save_changes_without_buttons_is_not_waiting():
    result = extract("", "Code", snapshot='dialog "Do you want to save the changes?"')
    assert result.status == "unknown" and "waiting_prompt" not in result.details


@pytest.mark.parametrize("prompt", ["Info: Continue?", "Info: Do you want to install the Python extension?"])
def test_text_info_question_alone_is_not_waiting(prompt):
    result = extract(prompt, "Code")
    assert result.status == "unknown" and "waiting_prompt" not in result.details


def test_text_install_prompt_requires_button_lines():
    result = extract("Info: Do you want to install the Python extension?\nInstall\nCancel", "Code")
    assert result.status == "waiting"
    assert result.details["waiting_prompt"] == "Do you want to install the Python extension?"


@pytest.mark.parametrize("snapshot_mode", [False, True])
@pytest.mark.parametrize("output, expected", [
    (["FAILED tests/a.py::t", "1 failed, 2 passed", "3 passed"], "unknown"),
    (["3 passed", "FAILED tests/a.py::t", "1 failed, 2 passed"], "error"),
    (["FAILED tests/a.py::t", "1 failed, 2 passed", "$ pytest", "3 passed"], "unknown"),
])
def test_only_last_terminal_run_determines_failures(snapshot_mode, output, expected):
    if snapshot_mode:
        snapshot = 'group "Terminal"\n' + "\n".join(f'  statictext "{line}"' for line in output)
        result = extract("", "Code", snapshot=snapshot)
    else:
        result = extract("Terminal\n" + "\n".join(output), "Code")
    assert result.status == expected
    assert ("failures" in result.details) is (expected == "error")


def test_successful_terminal_does_not_clear_current_problems():
    snapshot = ('group "Problems"\n  statictext "2 errors"\n'
                'group "Terminal"\n  statictext "1 failed, 2 passed"\n  statictext "3 passed"')
    result = extract("", "Code", snapshot=snapshot)
    assert result.status == "error" and result.details["failures"] == ["2 errors"]


@pytest.mark.parametrize("app", ["Code.exe", "code - oss", "code-oss"])
def test_additional_code_process_names(app):
    assert is_editor_app(app) and select_profile(app) == "editor"


def test_browser_app_outranks_editor_like_page_title():
    assert select_profile("Chrome", "Pricing - Cursor") == "browser"
    assert extract("Pricing", "Chrome", window_title="Pricing - Cursor").extractor == "browser"
