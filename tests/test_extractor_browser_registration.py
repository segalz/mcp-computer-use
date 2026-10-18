"""Browser profile wiring without modifying the browser implementation."""
import pytest

from mcp_computer_use.extractors import (
    PROFILES, browser_snapshot_details, extract, extract_browser, is_browser_app,
    select_profile, wants_snapshot,
)


@pytest.mark.parametrize("app", ["Chrome", "Google Chrome", "chrome.exe", "Edge", "msedge", "msedge.exe", "Microsoft Edge"])
def test_browser_names_select_and_dispatch(app):
    assert is_browser_app(app)
    assert select_profile(app) == "browser"
    assert wants_snapshot(app)
    assert extract("Page content", app).extractor == "browser"


@pytest.mark.parametrize("title", ["Pricing - Google Chrome", "Pricing - Microsoft Edge", "Pricing - \u200bMicrosoft Edge"])
def test_browser_title_selection_including_edge_zero_width(title):
    assert select_profile("Unknown App", title) == "browser"
    assert wants_snapshot("Unknown App", title)
    snapshot = f'window "{title}"\n  group "Page" id=RootWebArea\n    statictext "Page content"'
    result = extract("", "Unknown App", snapshot=snapshot)
    assert result.extractor == "browser" and result.last_message == "Page content"


def test_chrome_remote_desktop_is_not_a_browser():
    assert not is_browser_app("Chrome Remote Desktop")
    assert select_profile("Chrome Remote Desktop") == "generic"
    assert not wants_snapshot("Chrome Remote Desktop")
    assert extract("Remote session", "Chrome Remote Desktop").extractor == "generic"


def test_profile_registration_order():
    assert [name for name, _ in PROFILES][:4] == ["claude_code", "codex_desktop", "editor", "browser"]
    assert select_profile("Unknown App") == "generic"


def test_browser_public_helpers_are_reexported():
    assert extract_browser("Page content", "Chrome").extractor == "browser"
    assert browser_snapshot_details("") == {}


def test_snapshot_dispatch_passes_browser_content_and_details():
    snapshot = ('window "Pricing - Google Chrome"\n'
                '  group "Page" id=RootWebArea\n    statictext "Plans and pricing"')
    result = extract("Toolbar noise", "Chrome", snapshot=snapshot)
    assert result.extractor == "browser"
    assert result.last_message == "Plans and pricing"
    assert result.details["page_title"] == "Pricing"


@pytest.mark.parametrize("app, expected", [("Claude", "claude_code"), ("ChatGPT", "codex_desktop"), ("Code", "editor")])
def test_canonical_profiles_keep_precedence_over_browser_titles(app, expected):
    assert select_profile(app, "Docs - Google Chrome") == expected
