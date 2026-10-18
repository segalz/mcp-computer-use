"""Browser extractor profile (Chrome / Edge / Firefox / Brave).

Extracts page title, URL, active tab, page headings, failures, next steps,
and detects network-error pages while excluding browser chrome (toolbars,
favourites bar, tab strip).
"""
from __future__ import annotations

import re

from ._common import (
    Extraction, _cap, find_failures, find_next_step, parse_snapshot, _NODE, _LABEL
)
from .generic import extract_generic


KNOWN_BROWSER_APPS = {
    "chrome", "google chrome", "google-chrome",
    "msedge", "edge", "microsoft edge",
    "brave", "firefox", "mozilla firefox",
}

_BROWSER_SUFFIXES = re.compile(
    r"\s*-\s*(?:Google Chrome|Microsoft Edge|Mozilla Firefox|Brave)\s*$",
    re.I,
)
_EDGE_PROFILE_SEGMENT = re.compile(
    r"\s*-\s*(?:Profile\s*\d+|Personal|Work|Default|[A-Za-z0-9_-]+ Profile)\s*$",
    re.I,
)
_NETWORK_ERROR_PATTERN = re.compile(
    r"\b(?:network error|can't connect|dns_probe|err_connection|err_name_not_resolved|no internet)\b",
    re.I,
)
_HEADING_NON_TITLE = re.compile(
    r"^(?:Status:|Error:|Query\b|https?:|file:|Search\b\s*:)",
    re.I,
)

_CHROME_WORDS = {
    "back", "forward", "reload", "refresh", "home",
    "bookmark this tab", "favourites", "favorites",
    "settings and more", "settings and more (alt+f)",
    "new tab", "tab search", "search tabs", "close", "close tab",
    "minimise", "minimize", "maximise", "maximize",
    "address and search bar",
    "view site information", "view site info",
    "add this page to favourites", "add this page to favourites (ctrl+d)",
    "add this page to favorites", "add this page to favorites (ctrl+d)",
    "other favourites", "other favorites",
    "import favourites", "import favorites",
    "favourites bar", "favorites bar",
    "menu containing hidden favourites", "menu containing hidden favorites",
    "saved tab groups", "tab groups",
    "app bar", "toolbar", "separator",
    "chat", "copilot menu", "action required",
    "profile 1 profile", "personal profile",
    "sync your profile", "tab bar", "tabgroup",
}

_URL_PATTERN = re.compile(r"^(?:https?://|file:///|edge://|chrome://|about:|[a-zA-Z]:[/\\])", re.I)
_MEMORY_USAGE_TAB = re.compile(r"-\s*Memory usage\s*-\s*[\d.]+\s*[KMGT]?B", re.I)
_BOOKMARK_ITEM = re.compile(r"^(?:Example bookmark|Bookmark)\b", re.I)


def is_browser_app(app_name: str | None, window_title: str | None = None) -> bool:
    """Detect whether an application or window title represents a supported browser."""
    if isinstance(app_name, str) and app_name.strip():
        norm = app_name.strip().lower()
        if norm.endswith(".exe"):
            norm = norm[:-4]
        if norm in KNOWN_BROWSER_APPS:
            return True
    if isinstance(window_title, str) and window_title.strip():
        clean_title = window_title.replace("\u200b", "").strip().lower()
        if (clean_title.endswith(" - google chrome")
                or clean_title.endswith(" - microsoft edge")
                or clean_title == "microsoft edge"
                or clean_title.endswith(" microsoft edge")
                or clean_title.endswith(" - mozilla firefox")
                or clean_title.endswith(" - brave")):
            return True
    return False


def _clean_page_title(window_title: str) -> str:
    """Extract page title by stripping browser suffix and Edge profile segment."""
    title = window_title.replace("\u200b", "").strip()
    title = _BROWSER_SUFFIXES.sub("", title).strip()
    if title.lower() == "microsoft edge":
        return ""
    title = _EDGE_PROFILE_SEGMENT.sub("", title).strip()
    return title[:200]


def _extract_url_from_snapshot(snapshot: str) -> str | None:
    m = re.search(r'textfield "Address and search bar"[^"\n]*val="([^"]+)"', snapshot)
    if m:
        return m.group(1).strip()[:200]
    return None


def _extract_url_from_text(lines: list[str]) -> str | None:
    for line in lines[:25]:
        s = line.strip()
        if _URL_PATTERN.match(s):
            return s[:200]
    return None


def _find_web_area_nodes(snapshot: str) -> list[tuple[int, str, str]]:
    """Return nodes belonging strictly to the primary web document area."""
    lines = snapshot.splitlines()
    nodes_with_meta = []
    for ln in lines:
        m = _NODE.match(ln)
        if not m:
            continue
        indent, role, rest = len(m.group(1)), m.group(2), m.group(3)
        lm = _LABEL.match(rest)
        label = lm.group(1) if lm else ""
        is_root = "id=RootWebArea" in rest or "id=RootWebArea" in ln
        is_sync_dialog = "edge://sync-confirmation-dialog" in rest or "edge://sync-confirmation-dialog" in ln
        nodes_with_meta.append((indent, role, label, is_root, is_sync_dialog))

    # Skip sync/confirmation dialog web areas; prefer primary page RootWebArea
    primary_idx = None
    for i, (_, _, _, is_root, is_sync) in enumerate(nodes_with_meta):
        if is_root and not is_sync:
            primary_idx = i
            break
    if primary_idx is None:
        return []

    root_indent = nodes_with_meta[primary_idx][0]
    web_nodes = []
    for i in range(primary_idx + 1, len(nodes_with_meta)):
        if nodes_with_meta[i][0] <= root_indent:
            break
        web_nodes.append((nodes_with_meta[i][0], nodes_with_meta[i][1], nodes_with_meta[i][2]))
    return web_nodes


def _is_browser_chrome_line(line: str) -> bool:
    """Identify whether a flattened text line belongs to browser chrome/navigation."""
    s = line.replace("\u200b", "").strip()
    if not s:
        return True
    if s.lower() in _CHROME_WORDS:
        return True
    if _BROWSER_SUFFIXES.search(s) or s.lower() == "microsoft edge":
        return True
    if _MEMORY_USAGE_TAB.search(s):
        return True
    if _URL_PATTERN.match(s) or s.startswith("#"):
        return True
    if _BOOKMARK_ITEM.match(s):
        return True
    return False


def _filter_text_lines(lines: list[str]) -> list[str]:
    """Filter out browser chrome and address bar lines from flattened text."""
    return [line.strip() for line in lines if not _is_browser_chrome_line(line)]


def _is_heading(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 60:
        return False
    if re.search(r"[.!?]$", s):
        return False
    if _HEADING_NON_TITLE.match(s):
        return False
    return True


def _extract_headings_from_nodes(nodes: list[tuple[int, str, str]]) -> list[str]:
    headings: list[str] = []
    for _, role, label in nodes:
        clean = label.strip()
        if not clean:
            continue
        if role == "heading":
            if clean not in headings:
                headings.append(clean[:120])
        elif role == "statictext" and _is_heading(clean):
            if clean not in headings:
                headings.append(clean[:120])
    return headings[:5]


def _extract_headings_from_text(lines: list[str]) -> list[str]:
    headings: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if _is_heading(s) and s not in headings:
            headings.append(s[:120])
    return headings[:5]


def _is_network_error(nodes: list[tuple[int, str, str]], text_lines: list[str]) -> bool:
    for _, role, label in nodes:
        if role in {"group", "tab", "window", "heading"} and _NETWORK_ERROR_PATTERN.search(label):
            return True
    for line in text_lines:
        if _NETWORK_ERROR_PATTERN.search(line):
            return True
    return False


def browser_snapshot_details(snapshot: str) -> dict:
    """Extract browser details (title, URL, active tab, headings, failures) from snapshot."""
    if not isinstance(snapshot, str) or not snapshot.strip():
        return {}
    nodes = parse_snapshot(snapshot)
    details: dict = {}

    window_title = next((label for _, role, label in nodes if role == "window" and label.strip()), None)
    if window_title:
        title = _clean_page_title(window_title)
        if title:
            details["title"] = title
            details["page_title"] = title

    url = _extract_url_from_snapshot(snapshot)
    if url:
        details["url"] = url

    tab_label = next((label for _, role, label in nodes if role == "tab" and label.strip()), None)
    if tab_label:
        clean_tab = re.sub(r"\s*-\s*Memory usage.*$", "", tab_label, flags=re.I).strip()
        details["tab"] = clean_tab[:200]

    web_nodes = _find_web_area_nodes(snapshot)
    if web_nodes:
        headings = _extract_headings_from_nodes(web_nodes)
        if headings:
            details["headings"] = headings
        body_lines = [label.strip() for _, role, label in web_nodes if role in {"statictext", "link"} and label.strip()]
        failures = find_failures(body_lines)
        if failures:
            details["failures"] = failures
        next_step = find_next_step(body_lines)
        if next_step:
            details["next_step"] = next_step

    return details


def extract_browser(text: str | list[str], app: str, max_chars: int = 2000,
                    tail_chars: int = 2000, snapshot: str | None = None) -> Extraction:
    """Extract browser page content and metadata, ignoring chrome/toolbars."""
    lines = text if isinstance(text, list) else (text.splitlines() if isinstance(text, str) else [])
    raw_chars = len("\n".join(lines)) if isinstance(text, list) else len(str(text or ""))

    nodes = parse_snapshot(snapshot) if isinstance(snapshot, str) and snapshot.strip() else []
    details: dict = {}

    # Extract details from snapshot if available
    if isinstance(snapshot, str) and snapshot.strip():
        details.update(browser_snapshot_details(snapshot))

    # Extract / supplement from text if missing
    if "url" not in details:
        url = _extract_url_from_text(lines)
        if url:
            details["url"] = url

    # Determine status
    status = "error" if _is_network_error(nodes, lines) else "unknown"

    # Determine page content: prefer snapshot web area over flattened text
    web_nodes = _find_web_area_nodes(snapshot) if isinstance(snapshot, str) and snapshot.strip() else []
    if web_nodes:
        page_lines = [label.strip() for _, role, label in web_nodes if role in {"statictext", "link"} and label.strip()]
    else:
        # In text-only mode or when no web area is present in snapshot, filter out chrome lines
        page_lines = _filter_text_lines(lines)

    if not details.get("headings") and page_lines:
        headings = _extract_headings_from_text(page_lines)
        if headings:
            details["headings"] = headings

    if "failures" not in details and page_lines:
        failures = find_failures(page_lines)
        if failures:
            details["failures"] = failures

    if "next_step" not in details and page_lines:
        next_step = find_next_step(page_lines)
        if next_step:
            details["next_step"] = next_step

    joined_message = "\n".join(page_lines)
    message, truncated = _cap(joined_message, max_chars)

    return Extraction(app, "browser", status, message, raw_chars, truncated, details)
