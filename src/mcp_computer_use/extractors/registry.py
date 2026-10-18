"""App-name profile registry; matchers receive the lower-cased app name."""
from __future__ import annotations

from typing import Callable

from ._common import Extraction
from .claude_code import extract_claude, claude_snapshot_details
from .generic import extract_generic
from .codex_desktop import extract_codex_desktop, codex_snapshot_details, is_codex_app


# Preserve the mutable legacy registries and their first-match-wins behavior.
EXTRACTORS: list[tuple[Callable[[str], bool], Callable[..., Extraction]]] = []
SNAPSHOT_DETAILS: list[tuple[Callable[[str], bool], Callable[[str], dict]]] = []
PROFILES: list[tuple[str, Callable[[str], bool]]] = []


def register_profile(name: str, matcher: Callable[[str], bool],
                     extractor: Callable[..., Extraction] = extract_generic,
                     snapshot_details: Callable[[str], dict] | None = None) -> None:
    """Append a profile in selection order, with optional snapshot-detail support."""
    PROFILES.append((name, matcher))
    EXTRACTORS.append((matcher, extractor))
    if snapshot_details is not None:
        SNAPSHOT_DETAILS.append((matcher, snapshot_details))


def select_profile(app_name: str, window_title: str | None = None) -> str:
    """Select by app name only; window_title is reserved and does not affect selection."""
    for name, matcher in PROFILES:
        if matcher(app_name.lower()):
            return name
    return "generic"


register_profile("claude_code", lambda name: "claude" in name,
                 extract_claude, claude_snapshot_details)

register_profile("codex_desktop", is_codex_app, extract_codex_desktop, codex_snapshot_details)
