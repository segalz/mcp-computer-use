"""Profile selection preserves the original app-name dispatch and public imports."""
import pytest

from mcp_computer_use import extractors
from mcp_computer_use.extractors import Extraction, extract, select_profile, wants_snapshot
from mcp_computer_use.extractors import registry


@pytest.mark.parametrize("app, expected", [
    ("Claude", "claude_code"),
    ("CLAUDE", "claude_code"),
    ("cLaUdE", "claude_code"),
    ("Claude Desktop", "claude_code"),
    ("My Claude window", "claude_code"),
    ("Unknown App", "generic"),
    ("Notepad", "generic"),
    ("", "generic"),
])
def test_select_profile_by_app_name(app, expected):
    assert select_profile(app) == expected
    assert wants_snapshot(app) is (expected == "claude_code")


@pytest.mark.parametrize("app, title, expected", [
    ("Unknown App", "Claude Code", "generic"),
    ("Claude", "Unrelated title", "claude_code"),
])
def test_window_title_does_not_change_existing_selection(app, title, expected):
    assert select_profile(app, window_title=title) == expected


def test_registry_adds_a_profile_without_changing_import_sites(monkeypatch):
    # Isolate the mutable registries so no other test sees this temporary profile.
    for name in ("PROFILES", "EXTRACTORS", "SNAPSHOT_DETAILS"):
        isolated = list(getattr(registry, name))
        monkeypatch.setattr(registry, name, isolated)
        monkeypatch.setattr(extractors, name, isolated)

    def editor(lines, app, **kwargs):
        return Extraction(app, "test_editor", "unknown", "\n".join(lines), 0)

    registry.register_profile("test_editor", lambda name: name == "editor", editor,
                              lambda snapshot: {"project": "test"})
    assert select_profile("EDITOR") == "test_editor"
    assert wants_snapshot("EDITOR")
    result = extract("hello", "EDITOR", snapshot="snapshot")
    assert result.extractor == "test_editor" and result.last_message == "hello"
    assert result.details == {"project": "test"}
    assert select_profile("Claude") == "claude_code"


def test_legacy_api_reexports_and_fallback_stay_available():
    from mcp_computer_use.extractors import (
        _NODE, _LABEL, claude_status, claude_text_details, claude_snapshot_details,
        extract_claude, extract_generic, find_failures, find_next_step, parse_snapshot,
    )
    from mcp_computer_use.extractors import _common, claude_code, generic

    assert extractors.Extraction is _common.Extraction
    assert extract_claude is claude_code.extract_claude
    assert extract_generic is generic.extract_generic
    assert claude_status is claude_code.claude_status
    assert claude_text_details is claude_code.claude_text_details
    assert claude_snapshot_details is claude_code.claude_snapshot_details
    assert (find_failures, find_next_step, parse_snapshot) == (
        _common.find_failures, _common.find_next_step, _common.parse_snapshot,
    )
    assert (_NODE, _LABEL) == (_common._NODE, _common._LABEL)
    assert extractors.EXTRACTORS is registry.EXTRACTORS
    assert extractors.SNAPSHOT_DETAILS is registry.SNAPSHOT_DETAILS
    # A matching profile still falls back when it extracts no useful Claude reply.
    assert select_profile("Claude") == "claude_code"
    assert extract("plain text", "Claude").extractor == "generic"
