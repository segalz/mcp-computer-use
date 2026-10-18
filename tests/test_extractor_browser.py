"""Tests for the browser extractor profile (Chrome, Edge, Firefox, Brave)."""
from pathlib import Path
import pytest

from mcp_computer_use.extractors.browser import (
    extract_browser,
    browser_snapshot_details,
    is_browser_app,
)

FIXTURES = Path(__file__).parent / "fixtures" / "browser"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Fixture 1: chrome_dashboard_snapshot.txt (≥ 3 tests)
# -----------------------------------------------------------------------------

def test_chrome_snapshot_metadata():
    snap = fixture("chrome_dashboard_snapshot.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert res.extractor == "browser"
    assert res.status == "unknown"
    assert res.details["title"] == "Build dashboard - Example"
    assert res.details["page_title"] == "Build dashboard - Example"
    assert res.details["url"] == "C:/work/site/index.html"
    assert res.details["tab"] == "Build dashboard - Example"
    assert res.details["headings"] == ["Build dashboard", "Latest run #42", "Search", "סיכום"]


def test_chrome_snapshot_failures_and_next_step():
    snap = fixture("chrome_dashboard_snapshot.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert any("2 tests failed" in f for f in res.details["failures"])
    assert "Error: 2 tests failed in parser_test.py" in res.details["failures"]
    assert res.details["next_step"] == "השלב הבא: לתקן את הבדיקות."


def test_chrome_snapshot_page_content_excludes_browser_chrome():
    snap = fixture("chrome_dashboard_snapshot.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert "Build dashboard" in res.last_message
    assert "Latest run #42" in res.last_message
    for chrome_control in ["Minimise", "Maximise", "Back", "Forward", "Reload", "Tab search", "New tab"]:
        assert chrome_control not in res.last_message


# -----------------------------------------------------------------------------
# Fixture 2: chrome_dashboard_interactive.txt (≥ 3 tests)
# -----------------------------------------------------------------------------

def test_chrome_interactive_metadata():
    snap = fixture("chrome_dashboard_interactive.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert res.extractor == "browser"
    assert res.status == "unknown"
    assert res.details["title"] == "Build dashboard - Example"
    assert res.details["url"] == "C:/work/site/index.html"
    assert res.details["tab"] == "Build dashboard - Example"


def test_chrome_interactive_no_headings_or_failures():
    snap = fixture("chrome_dashboard_interactive.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert "headings" not in res.details
    assert "failures" not in res.details


def test_chrome_interactive_empty_page_content():
    snap = fixture("chrome_dashboard_interactive.txt")
    res = extract_browser("", "Google Chrome", snapshot=snap)
    assert res.last_message == ""
    assert "Back" not in res.last_message


# -----------------------------------------------------------------------------
# Fixture 3: chrome_dashboard_text.txt (≥ 3 tests)
# -----------------------------------------------------------------------------

def test_chrome_text_url_and_headings():
    text = fixture("chrome_dashboard_text.txt")
    res = extract_browser(text, "Google Chrome")
    assert res.extractor == "browser"
    assert res.status == "unknown"
    assert res.details["url"] == "C:/work/site/index.html"
    assert res.details["headings"] == ["Build dashboard", "Latest run #42", "Search", "סיכום"]


def test_chrome_text_failures_and_next_step():
    text = fixture("chrome_dashboard_text.txt")
    res = extract_browser(text, "Google Chrome")
    assert any("2 tests failed" in f for f in res.details["failures"])
    assert res.details["next_step"] == "השלב הבא: לתקן את הבדיקות."


def test_chrome_text_page_content_retained():
    text = fixture("chrome_dashboard_text.txt")
    res = extract_browser(text, "Google Chrome")
    assert "Build dashboard" in res.last_message
    assert "Latest run #42" in res.last_message
    assert "סיכום" in res.last_message
    # URL lines and repeats must not appear in page message
    assert "C:/work/site/index.html" not in res.last_message
    assert "file:///" not in res.last_message


# -----------------------------------------------------------------------------
# Fixture 4: edge_error_snapshot.txt (≥ 3 tests)
# -----------------------------------------------------------------------------

def test_edge_network_error_status():
    snap = fixture("edge_error_snapshot.txt")
    res = extract_browser("", "Microsoft Edge", snapshot=snap)
    assert res.extractor == "browser"
    assert res.status == "error"


def test_edge_metadata():
    snap = fixture("edge_error_snapshot.txt")
    res = extract_browser("", "Microsoft Edge", snapshot=snap)
    assert res.details["title"] == "example.com"
    assert res.details["url"] == "https://example.com"
    assert res.details["tab"] == "example.com - Network error"


def test_edge_page_content_excludes_favourites_and_toolbars():
    snap = fixture("edge_error_snapshot.txt")
    res = extract_browser("", "Microsoft Edge", snapshot=snap)
    assert "Example bookmark" not in res.last_message
    assert "Favourites bar" not in res.last_message
    assert "App bar" not in res.last_message
    for control in ["Back", "Refresh", "Home", "Favourites", "Copilot"]:
        assert control not in res.last_message


# -----------------------------------------------------------------------------
# Additional tests: is_browser_app, standalone snapshot details, robustness
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("app", [
    "Google Chrome", "chrome", "CHROME", "chrome.exe",
    "Microsoft Edge", "msedge", "MSEDGE", "msedge.exe",
    "Brave", "brave.exe", "Firefox", "firefox.exe", "Mozilla Firefox",
])
def test_is_browser_app_by_name(app):
    assert is_browser_app(app)


@pytest.mark.parametrize("title", [
    "Build dashboard - Example - Google Chrome",
    "example.com - Profile 1 - Microsoft\u200b Edge",  # includes U+200B zero-width space
    "example.com - Profile 1 - Microsoft Edge",
    "Microsoft\u200b Edge",
    "Microsoft Edge",
    "Dashboard - Mozilla Firefox",
    "Project - Brave",
])
def test_is_browser_app_by_window_title_including_u200b(title):
    assert is_browser_app("Unknown App", window_title=title)


@pytest.mark.parametrize("app, title", [
    ("Notepad", "notes.txt - Notepad"),
    ("Claude", "Claude Code"),
    ("ChatGPT", "ChatGPT"),
    ("Terminal", "PowerShell"),
    (None, None),
    ("", ""),
])
def test_is_browser_app_negative_cases(app, title):
    assert not is_browser_app(app, window_title=title)


def test_browser_snapshot_details_standalone():
    snap = fixture("chrome_dashboard_snapshot.txt")
    details = browser_snapshot_details(snap)
    assert details["title"] == "Build dashboard - Example"
    assert details["url"] == "C:/work/site/index.html"
    assert details["tab"] == "Build dashboard - Example"
    assert len(details["headings"]) <= 5
    assert any("2 tests failed" in f for f in details["failures"])


@pytest.mark.parametrize("garbage", [None, "", "   ", "not an accessibility tree", "[@e1] invalid\x00data"])
def test_browser_robustness_never_crashes(garbage):
    res = extract_browser(garbage or "", "Google Chrome", snapshot=garbage)
    assert res.status == "unknown"
    assert isinstance(res.details, dict)
    assert isinstance(res.last_message, str)


def test_browser_bounded_outputs():
    long_string = "a" * 1000
    snap = f'window "{long_string} - Google Chrome"\n  textfield "Address and search bar" val="{long_string}"\n  tab "{long_string}"\n  textarea "{long_string}" id=RootWebArea\n    statictext "{long_string}"\n'
    res = extract_browser("", "Google Chrome", max_chars=50, snapshot=snap)
    assert len(res.details["title"]) <= 200
    assert len(res.details["url"]) <= 200
    assert len(res.details["tab"]) <= 200
    assert all(len(h) <= 120 for h in res.details.get("headings", []))
    assert len(res.details.get("headings", [])) <= 5
    assert len(res.last_message) <= 50
    assert res.truncated


# -----------------------------------------------------------------------------
# Regression tests: mixed chrome in text mode & preferring snapshot web area
# -----------------------------------------------------------------------------

def test_flattened_text_mixed_chrome_and_page_yields_only_page_lines():
    mixed_text = (
        "Build dashboard - Example - Google Chrome\n"
        "Back\n"
        "Forward\n"
        "Reload\n"
        "Address and search bar\n"
        "C:/work/site/index.html\n"
        "file:///C:/work/site/index.html\n"
        "Bookmark this tab\n"
        "Favourites bar\n"
        "Example bookmark 1\n"
        "Example bookmark 2\n"
        "Example bookmark 3\n"
        "Tab search\n"
        "Build dashboard - Example - Memory usage - 20.4 MB\n"
        "Close tab\n"
        "New tab\n"
        "Build dashboard\n"
        "file:///C:/work/site/index.html#runs\n"
        "file:///C:/work/site/index.html#settings\n"
        "Latest run #42\n"
        "Status: Failed\n"
        "Error: 2 tests failed in parser_test.py\n"
        "Search\n"
        "Query\n"
        "סיכום\n"
        "הבנייה נכשלה. השלב הבא: לתקן את הבדיקות.\n"
    )
    res = extract_browser(mixed_text, "Google Chrome")
    assert res.extractor == "browser"

    # Verify no browser chrome / URL repeat words leaked into last_message
    for chrome_word in [
        "Google Chrome", "Back", "Forward", "Reload", "Address and search bar",
        "Bookmark this tab", "Favourites bar", "Example bookmark 1",
        "Example bookmark 2", "Example bookmark 3", "Tab search", "Memory usage",
        "Close tab", "New tab", "C:/work/site/index.html", "file:///",
        "#runs", "#settings",
    ]:
        assert chrome_word not in res.last_message

    expected_page_lines = [
        "Build dashboard",
        "Latest run #42",
        "Status: Failed",
        "Error: 2 tests failed in parser_test.py",
        "Search",
        "Query",
        "סיכום",
        "הבנייה נכשלה. השלב הבא: לתקן את הבדיקות.",
    ]
    assert res.last_message == "\n".join(expected_page_lines)
    assert res.details["url"] == "C:/work/site/index.html"
    assert res.details["headings"] == ["Build dashboard", "Latest run #42", "Search", "סיכום"]
    assert any("2 tests failed" in f for f in res.details["failures"])
    assert res.details["next_step"] == "השלב הבא: לתקן את הבדיקות."


def test_edge_flattened_text_chrome_dropped():
    edge_text = (
        "example.com - Profile 1 - Microsoft\u200b Edge\n"
        "Back\nRefresh\nHome\n"
        "Address and search bar\n"
        "https://example.com\n"
        "Favourites\n"
        "Settings and more\n"
        "Favourites bar\n"
        "Example bookmark 1\n"
        "Example bookmark 2\n"
        "Tab bar\n"
        "Search tabs\n"
        "New Tab\n"
        "Actual page content line 1\n"
        "Actual page content line 2\n"
    )
    res = extract_browser(edge_text, "Microsoft Edge")
    assert "Back" not in res.last_message
    assert "Refresh" not in res.last_message
    assert "Home" not in res.last_message
    assert "Favourites" not in res.last_message
    assert "Example bookmark" not in res.last_message
    assert "https://example.com" not in res.last_message
    assert res.details["url"] == "https://example.com"
    assert res.last_message == "Actual page content line 1\nActual page content line 2"


def test_snapshot_web_area_preferred_over_flattened_text():
    noisy_flattened_text = "Corrupted line 1\nNoisy line 2\nOutdated text"
    snap = fixture("chrome_dashboard_snapshot.txt")
    res = extract_browser(noisy_flattened_text, "Google Chrome", snapshot=snap)
    assert "Corrupted line" not in res.last_message
    assert "Noisy line" not in res.last_message
    assert "Build dashboard" in res.last_message
    assert "Latest run #42" in res.last_message
