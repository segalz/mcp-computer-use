"""Conservative VS Code-family metadata, prompt, Problems and terminal extraction."""
from __future__ import annotations

import re

from ._common import Extraction, _cap, find_failures, parse_snapshot
from .generic import extract_generic


_APPS = {"code", "code.exe", "code - oss", "code-oss", "code - insiders", "cursor", "antigravity ide", "windsurf", "vscodium"}
# Windsurf is assumed to use the VS Code-style file - workspace - product title.
_TITLE_PRODUCTS = ("Visual Studio Code", "Cursor", "Antigravity IDE", "Windsurf", "VSCodium", "Code - Insiders")
_EXPLORER = re.compile(r"^Explorer Section:\s*(.+)$", re.I)
_NEXT = re.compile(r"^(?:Next steps?|השלב הבא)\s*:\s*\S.*$", re.I)
_QUESTION = re.compile(r"(?:Do you trust the authors|Do you want to install|Would you like to install|Do you want to enable)[^?]*\?", re.I)
_PANEL = re.compile(r"^(?:Problems|Terminal|Integrated terminal)(?:$|\s*[(:])", re.I)
_DIAGNOSTIC = re.compile(r"\b[A-Za-z_]\w*(?:Error|Exception):")
_ERROR_COUNT = re.compile(r"\b[1-9]\d*\s+(?:errors?|failures?|failed)\b", re.I)
_FILE = re.compile(r"[^\s/\\]+\.[A-Za-z0-9]{1,12}$")
_NOISE = {"checked", "unchecked", "Explorer", "Outline", "Timeline", "Source Control", "Extensions",
          "Run and Debug", "File", "Edit", "Selection", "View", "Go", "Run", "Help", "No Problems",
          "Minimize", "Maximize", "Close", "Terminal", "Problems"}


def is_editor_app(name: str) -> bool:
    return name.strip().lower() in _APPS


def is_editor_title(title: str | None) -> bool:
    if not isinstance(title, str):
        return False
    lower = title.strip().lower()
    return (" - antigravity ide - " in lower
            or any(lower.endswith(" - " + product.lower()) for product in _TITLE_PRODUCTS))


def editor_title_details(title: str | None) -> dict:
    if not isinstance(title, str):
        return {}
    title = title.strip()
    middle = re.search(r" - Antigravity IDE - ", title, re.I)
    if middle:
        project, file = title[:middle.start()].strip(), title[middle.end():].strip()
        return {key: value[:200] for key, value in (("project", project), ("file", file)) if value}
    for product in _TITLE_PRODUCTS:
        suffix = " - " + product
        if title.lower().endswith(suffix.lower()):
            content = title[:-len(suffix)].strip()
            if " - " in content:
                file, project = content.rsplit(" - ", 1)
                return {key: value.strip()[:200] for key, value in (("project", project), ("file", file)) if value.strip()}
            if content:
                return {"file" if _FILE.fullmatch(content) else "project": content[:200]}
    return {}


def _scope(nodes: list[tuple[int, str, str]], index: int) -> list[tuple[int, str, str]]:
    end = index + 1
    while end < len(nodes) and nodes[end][0] > nodes[index][0]:
        end += 1
    return nodes[index + 1:end]


def _question(label: str, *, allow_save: bool = False) -> str | None:
    match = _QUESTION.search(label)
    if match:
        return match[0][:200]
    if re.fullmatch(r"Install (?:the )?extension\?", label.strip(), re.I):
        return label.strip()[:200]
    if allow_save and re.search(r"Do you want to save\b.*\?", label, re.I):
        return label.strip()[:200]
    return None


def _snapshot_info(nodes: list[tuple[int, str, str]]) -> tuple[dict, list[str], str | None]:
    details = {}
    title = next((label for _, role, label in nodes if role == "window" and is_editor_title(label)), None)
    details.update(editor_title_details(title))
    file_tabs = []
    evidence = []
    prompts = []
    for index, (_, role, label) in enumerate(nodes):
        explorer = _EXPLORER.fullmatch(label) if role in {"button", "statictext", "tree"} else None
        if explorer:
            details.setdefault("project", explorer[1][:200])
        if role == "tab" and _FILE.fullmatch(label):
            file_tabs.append(label)
        if role in {"dialog", "alertdialog", "window", "alert"} and not is_editor_title(label):
            prompt = _question(label, allow_save=role in {"dialog", "alertdialog", "alert"})
            children = _scope(nodes, index)
            buttons = [name for _, child_role, name in children if child_role == "button"]
            if prompt and buttons:
                prompts.append((0 if "trust the authors" in prompt.lower() else 1, prompt))
        if role in {"group", "region", "tabpanel", "list", "tree"} and _PANEL.match(label):
            panel_lines = [name for _, child_role, name in _scope(nodes, index)
                           if child_role in {"statictext", "text", "listitem", "treeitem", "status"} and name]
            evidence.extend(_last_terminal_run(panel_lines) if "terminal" in label.lower() else panel_lines)
        elif role in {"button", "status"} and _ERROR_COUNT.search(label):
            evidence.append(label)
    if "file" not in details and len(set(file_tabs)) == 1:
        details["file"] = file_tabs[0][:200]
    prompt = min(prompts, key=lambda item: item[0])[1] if prompts else None
    return details, evidence, prompt


def _last_terminal_run(lines: list[str]) -> list[str]:
    """Discard output superseded by a new shell command or later test summary."""
    start = 0
    summaries = []
    for index, line in enumerate(lines):
        if re.match(r"^(?:\$\s+|PS\s+.*>\s*|[A-Za-z]:[\\/].*>\s*)", line):
            start = index
        if re.search(r"\b\d+\s+(?:passed|failed)\b", line, re.I):
            summaries.append(index)
    summaries = [index for index in summaries if index >= start]
    if len(summaries) > 1:
        start = max(start, summaries[-2] + 1)
    if summaries and not _ERROR_COUNT.search(lines[summaries[-1]]):
        start = max(start, summaries[-1])
    return lines[start:]


def _text_info(lines: list[str]) -> tuple[dict, list[str], str | None]:
    details = {}
    clean = [line.strip() for line in lines if line.strip()]
    for line in clean:
        if is_editor_title(line):
            details.update(editor_title_details(line))
            break
    evidence = []
    panel_lines = []
    panel = None
    menu_words = {"File", "Edit", "Selection", "View", "Go", "Run", "Terminal", "Help"}
    for index, line in enumerate(clean):
        explorer = _EXPLORER.fullmatch(line)
        if explorer:
            details.setdefault("project", explorer[1][:200])
        # Flattened menu items have no roles. Never open a panel from that run.
        left = index
        while left > 0 and clean[left - 1] in menu_words:
            left -= 1
        right = index + 1
        while right < len(clean) and clean[right] in menu_words:
            right += 1
        in_menu = line in menu_words and right - left >= 3
        heading = _PANEL.match(line) and not in_menu
        if heading or line in {"Output", "Debug Console", "Explorer", "Outline", "Timeline"}:
            evidence.extend(_last_terminal_run(panel_lines) if panel == "terminal" else panel_lines)
            panel_lines = []
            panel = ("terminal" if "terminal" in line.lower() else "problems") if heading else None
            continue
        if panel and not in_menu:
            panel_lines.append(line)
    evidence.extend(_last_terminal_run(panel_lines) if panel == "terminal" else panel_lines)
    # Flattened text lacks button roles: require corroborating dialog controls.
    for index, line in enumerate(clean):
        prompt = _question(line)
        context = clean[max(0, index - 5):index + 6]
        if prompt and "trust the authors" in prompt.lower():
            if "Trust folder and enable all features" in context and "Browse folder in restricted mode" in context:
                return details, evidence, prompt
    for index, line in enumerate(clean):
        prompt = _question(line)
        buttons = set(clean[index + 1:index + 6])
        if prompt and "Install" in buttons and {"Cancel", "Show Recommendations", "Not Now"} & buttons:
            return details, evidence, prompt
    return details, evidence, None


def _meaningful(lines: list[str]) -> list[str]:
    output = []
    for line in lines:
        line = line.strip()
        if (not line or line in _NOISE or is_editor_title(line) or _EXPLORER.fullmatch(line)
                or line.startswith(("vscode-file:", "app://")) or re.match(r"^[A-Za-z]:[\\/]", line)
                or not any(character.isalnum() for character in line)):
            continue
        if not output or output[-1] != line:
            output.append(line)
    return output


def editor_snapshot_details(snapshot: str) -> dict:
    nodes = parse_snapshot(snapshot) if isinstance(snapshot, str) else []
    return extract_editor([], "Code", snapshot_nodes=nodes).details


def extract_editor(lines: list[str], app: str, max_chars: int = 2000,
                   tail_chars: int = 2000, snapshot: str | None = None,
                   window_title: str | None = None,
                   snapshot_nodes: list[tuple[int, str, str]] | None = None, **_) -> Extraction:
    nodes = snapshot_nodes if snapshot_nodes is not None else (parse_snapshot(snapshot) if isinstance(snapshot, str) else [])
    details, text_evidence, text_prompt = _text_info(lines)
    snapshot_details, snapshot_evidence, snapshot_prompt = _snapshot_info(nodes)
    details.update(snapshot_details)
    details.update(editor_title_details(window_title))
    evidence = snapshot_evidence if snapshot_evidence else text_evidence
    diagnostics = ["Error: " + line if _DIAGNOSTIC.search(line) else line for line in evidence]
    failures = find_failures(diagnostics, limit=5)
    if failures:
        details["failures"] = failures
    prompt = snapshot_prompt or text_prompt
    if prompt:
        details.update(waiting_for_user=True, waiting_prompt=prompt[:200])
    next_steps = [line.strip() for line in evidence if _NEXT.fullmatch(line.strip())]
    if next_steps:
        details["next_step"] = " ".join(next_steps[-2:])[:200]
    status = "waiting" if prompt else "error" if failures else "unknown"
    message_lines = ([prompt] if prompt else []) + evidence
    if not message_lines:
        message_lines = _meaningful(lines if lines else [label for _, role, label in nodes if role == "statictext"])
    fallback = extract_generic(message_lines, app, max_chars=max(2, max_chars), tail_chars=tail_chars)
    limit = max(0, max_chars)
    if limit <= 1:
        message, capped = fallback.last_message[:limit], len(fallback.last_message) > limit
    else:
        message, capped = _cap(fallback.last_message, limit)
    return Extraction(app, "editor", status, message, len("\n".join(lines)), fallback.truncated or capped, details)
