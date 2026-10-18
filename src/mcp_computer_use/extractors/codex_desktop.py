"""Conservative Codex Desktop extraction; only home fixtures are real captures.

Snapshot roles delimit UI controls and message content. Flattened thread text
without an explicit assistant boundary is retained as unclassified content,
with unknown status rather than an inferred user/assistant split.
"""
from __future__ import annotations

import re

from ._common import Extraction, _cap, find_failures, parse_snapshot
from .generic import extract_generic


_MODE = re.compile(r"^Switch mode, current mode:\s*(.+)$", re.I)
_HOME = re.compile(r"^What should we work on in\s+(.+?)\??$", re.I)
_ACTIVE = re.compile(r"^(?:Thinking|Working)(?:\s*[.…]+)?$", re.I)
_APPROVAL = re.compile(r"^(?:Allow command\?|Approve command\?|Approval required|Permission required)$", re.I)
_NEXT_STEP = re.compile(r"^(?:Next steps?|השלב הבא)\s*:\s*\S.*$", re.I)
_CONTROLS = {"copy", "good response", "bad response", "stop", "approve", "deny", "allow", "allow once", "always allow"}
_COMPOSER = {"Message ChatGPT", "Do anything"}
_ASSISTANT_MARKERS = {"Assistant:", "Assistant turn", "Codex said:"}
_FIELDS = {"Project": "project", "Workspace": "project", "Session": "session",
           "Thread": "session", "Directory": "folder", "Branch": "branch"}


def is_codex_app(name: str) -> bool:
    return name.strip().lower() in {"chatgpt", "codex", "codex desktop", "openai.codex"}


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    limit = max(0, limit)
    if limit <= 1:
        return text.strip()[:limit], len(text.strip()) > limit
    return _cap(text, limit)


def _scope(nodes: list[tuple[int, str, str]], index: int) -> list[tuple[int, str, str]]:
    indent = nodes[index][0]
    end = index + 1
    while end < len(nodes) and nodes[end][0] > indent:
        end += 1
    return nodes[index + 1:end]


def _last_group(nodes: list[tuple[int, str, str]], label: str) -> int | None:
    return next((index for index in range(len(nodes) - 1, -1, -1)
                 if nodes[index][1] in {"group", "dialog"} and nodes[index][2] == label), None)


def _fields(labels: list[str]) -> dict:
    details = {}
    for label in labels:
        key, separator, value = label.partition(": ")
        if separator and key in _FIELDS and value.strip():
            details[_FIELDS[key]] = value.strip()
    return details


def _bound_details(details: dict) -> dict:
    for key in ("project", "session", "folder", "mode", "branch"):
        if key in details:
            details[key] = details[key][:120]
    return details


def _body_details(body: list[str]) -> dict:
    details = {}
    failures = find_failures(body)
    if failures:
        details["failures"] = failures
    next_steps = [line.strip() for line in body if _NEXT_STEP.fullmatch(line.strip())]
    if next_steps:
        details["next_step"] = " ".join(next_steps[-2:])[:300]
    return details


def _waiting(labels: list[str], controls: list[str]) -> str | None:
    buttons = set(controls)
    paired = "deny" in buttons and bool(buttons & {"approve", "allow", "allow once", "always allow"})
    if not paired:
        return None
    for index, label in enumerate(labels):
        if _APPROVAL.fullmatch(label):
            return next((line for line in labels[index + 1:]
                         if line and line.casefold() not in _CONTROLS), label)[:240]
    return None


def _control_state(controls: list[str], prompt: str | None, has_body: bool) -> str:
    stops = [index for index, label in enumerate(controls) if label == "stop"]
    copies = [index for index, label in enumerate(controls) if label == "copy"]
    feedback = [index for index, label in enumerate(controls) if label in {"good response", "bad response"}]
    finished = bool(has_body and copies and feedback)
    if prompt:
        return "unknown" if stops or finished else "waiting"
    if finished:
        # Completion controls following a stale Stop are the newer evidence.
        if not stops or (max(copies) > max(stops) and max(feedback) > max(stops)):
            return "finished"
        return "unknown"
    return "working" if stops else "unknown"


def _text_info(lines: list[str]) -> tuple[dict, list[str], str]:
    clean = [line.strip() for line in lines if line.strip()]
    user = next((index for index in range(len(clean) - 1, -1, -1)
                 if clean[index] == "You said:"), None)
    header = clean[:user] if user is not None else clean
    details = _fields(header)
    for line in header:
        home = _HOME.fullmatch(line)
        if home:
            details["project"] = home[1].rstrip("?").strip()
        mode = _MODE.fullmatch(line)
        if mode:
            details["mode"] = mode[1]
    if user is None:
        return details, [], "unknown"
    tail = clean[user + 1:]
    composer = next((index for index in range(len(tail) - 1, -1, -1) if tail[index] in _COMPOSER), None)
    end = composer if composer is not None else len(tail)
    start = end
    # Only a contiguous trailing region adjacent to a composer can be controls.
    while start > 0 and end - start < 8 and tail[start - 1].casefold() in _CONTROLS:
        start -= 1
    controls = [line.casefold() for line in tail[start:end]] if composer is not None else []
    marker = next((index for index in range(start - 1, -1, -1) if tail[index] in _ASSISTANT_MARKERS), None)
    if marker is None:
        body = tail[:start]  # Unclassified content: do not guess how many user lines there are.
        details.update(_body_details(body))
        return details, body, "unknown"
    body = tail[marker + 1:start]
    details.update(_body_details(body))
    prompt = _waiting(body, controls)
    status = _control_state(controls, prompt, bool(body))
    if status == "waiting":
        details.update(waiting_for_user=True, waiting_prompt=prompt)
    return details, body, status


def _sidebar_details(nodes: list[tuple[int, str, str]]) -> dict:
    """Read sidebar labels only, corroborated by their nested action controls."""
    details = {}
    projects = []
    chats = []
    recent_chats = []
    has_recents = False
    for index, (_, role, label) in enumerate(nodes):
        if role not in {"group", "navigation", "region"} or label.casefold() not in {"home", "sidebar"}:
            continue
        sidebar = _scope(nodes, index)
        in_recents = False
        skip_until = -1
        for position, (_, child_role, name) in enumerate(sidebar):
            if position < skip_until:
                continue
            if child_role == "group" and name in {"Conversation", "Assistant turn", "User turn"}:
                skip_until = position + 1 + len(_scope(sidebar, position))
                continue
            if child_role == "button" and name == "View activity, needs attention":
                details["needs_attention"] = True
            if child_role == "button" and name == "Recents":
                has_recents = in_recents = True
            if child_role not in {"listitem", "button"} or not name.strip():
                continue
            children = _scope(sidebar, position)
            controls = {value for _, control_role, value in children if control_role == "button"}
            if child_role == "button" and not children:
                # Some snapshots flatten the action buttons beside the label.
                for depth, control_role, value in sidebar[position + 1:position + 3]:
                    if depth != sidebar[position][0] or control_role != "button":
                        break
                    controls.add(value)
            if "Project actions for " + name in controls:
                if name not in projects:
                    projects.append(name)
            elif {"Pin chat", "Archive chat"} <= controls:
                if name not in chats:
                    chats.append(name)
                if in_recents and name not in recent_chats:
                    recent_chats.append(name)
    if projects:
        details["projects"] = [name[:80] for name in projects[:8]]
    selected_chats = recent_chats if has_recents else chats
    if selected_chats:
        details["recent_chats"] = [name[:80] for name in selected_chats[:5]]
    return details


def _snapshot_info(nodes: list[tuple[int, str, str]]) -> tuple[dict, list[str], str, bool]:
    details = _sidebar_details(nodes)
    for index, (_, role, label) in enumerate(nodes):
        if role == "button" and label == "View activity, needs attention":
            details["needs_attention"] = True
        mode = _MODE.fullmatch(label) if role == "button" else None
        if mode:
            details["mode"] = mode[1]
        if role == "button" and label.startswith("Change project: "):
            details["project"] = label[len("Change project: "):]
        if role == "statictext" and label.startswith("What should we work on in "):
            home = _HOME.fullmatch(label.strip())
            if home:
                details.setdefault("project", home[1].rstrip("?").strip())
            elif index + 1 < len(nodes) and nodes[index + 1][1] == "button":
                details.setdefault("project", nodes[index + 1][2].rstrip("?").strip())
    header_index = _last_group(nodes, "Thread header")
    if header_index is not None:
        header = _scope(nodes, header_index)
        details.update(_fields([label for _, role, label in header if role in {"button", "statictext"}]))
        title = next((label for _, role, label in header if role == "heading" and label), None)
        if title:
            details["session"] = title
        folder_name = re.split(r"[\\/]+", details.get("folder", "").rstrip("\\/"))[-1]
        project = next((label for _, role, label in header if role == "button" and label and label == folder_name), None)
        if project:
            details["project"] = project
    conversation_index = _last_group(nodes, "Conversation")
    recognized = bool(details) or conversation_index is not None
    if conversation_index is None:
        return details, [], "unknown", recognized
    conversation = _scope(nodes, conversation_index)
    assistant_index = _last_group(conversation, "Assistant turn")
    user_index = _last_group(conversation, "User turn")
    if assistant_index is None or (user_index is not None and user_index > assistant_index):
        return details, [], "unknown", True
    assistant = _scope(conversation, assistant_index)
    body = [label.strip() for _, role, label in assistant if role == "statictext" and label.strip()]
    details.update(_body_details(body))
    # Keep document order so later completion controls can supersede an old Stop.
    control_nodes = [(index, label.casefold()) for index, (_, role, label) in enumerate(nodes)
                     if role == "button" and label.casefold() in _CONTROLS
                     and (assistant_index + conversation_index + 1 < index)]
    assistant_start = conversation_index + 1 + assistant_index
    assistant_end = assistant_start + 1 + len(assistant)
    composer_index = _last_group(nodes, "Composer utility bar")
    composer_end = (composer_index + 1 + len(_scope(nodes, composer_index))) if composer_index is not None else -1
    control_nodes = [(index, label) for index, label in control_nodes
                     if assistant_start < index < assistant_end
                     or (composer_index is not None and composer_index < index < composer_end)]
    controls = [label for _, label in control_nodes]
    approval_index = _last_group(assistant, "Approval prompt")
    approval = _scope(assistant, approval_index) if approval_index is not None else []
    prompt = _waiting([label.strip() for _, role, label in approval if role == "statictext" and label.strip()],
                      [label.casefold() for _, role, label in approval if role == "button"])
    status = _control_state(controls, prompt, bool(body))
    # Thinking/Working prose inside an assistant turn is never a status signal.
    conversation_end = conversation_index + 1 + len(conversation)
    message_scopes = [(conversation_index + 1 + index,
                       conversation_index + 2 + index + len(_scope(conversation, index)))
                      for index, (_, role, label) in enumerate(conversation)
                      if role == "group" and label in {"Assistant turn", "User turn"}]
    active = [index for index, (_, role, label) in enumerate(nodes)
              if role in {"status", "progressbar"} and _ACTIVE.fullmatch(label.strip())
              and (conversation_index < index < conversation_end
                   or (composer_index is not None and composer_index < index < composer_end))
              and not any(start < index < end for start, end in message_scopes)
              and (user_index is None or index > conversation_index + 1 + user_index)]
    if active:
        completion_after = (status == "finished"
                            and any(index > max(active) and label == "copy" for index, label in control_nodes)
                            and any(index > max(active) and label in {"good response", "bad response"}
                                    for index, label in control_nodes))
        conflicted = status == "unknown" and (prompt is not None or
                    (bool(body) and "copy" in controls and bool({"good response", "bad response"} & set(controls))))
        if not completion_after:
            status = "unknown" if conflicted or status in {"finished", "waiting"} else "working"
    if status == "waiting":
        details.update(waiting_for_user=True, waiting_prompt=prompt)
    return details, body, status, True


def codex_snapshot_details(snapshot: str) -> dict:
    nodes = parse_snapshot(snapshot) if isinstance(snapshot, str) else []
    return _bound_details(_snapshot_info(nodes)[0])


def extract_codex_desktop(lines: list[str], app: str, max_chars: int = 2000,
                          tail_chars: int = 2000, snapshot: str | None = None,
                          snapshot_nodes: list[tuple[int, str, str]] | None = None, **_) -> Extraction:
    nodes = snapshot_nodes if snapshot_nodes is not None else (parse_snapshot(snapshot) if isinstance(snapshot, str) else [])
    details, text_body, text_status = _text_info(lines)
    snapshot_details, snapshot_body, snapshot_status, recognized = _snapshot_info(nodes)
    if recognized:
        details = {key: value for key, value in details.items() if key not in {"waiting_for_user", "waiting_prompt"}}
        details.update(snapshot_details)
        status = snapshot_status
        if text_status != "unknown" and snapshot_status != "unknown" and text_status != snapshot_status:
            status = "unknown"
            details.pop("waiting_for_user", None)
            details.pop("waiting_prompt", None)
    else:
        status = text_status
    body = snapshot_body or text_body
    if body:
        message, truncated = _bounded("\n".join(body), max_chars)
    else:
        fallback = extract_generic(lines, app, max_chars=max(2, max_chars), tail_chars=tail_chars)
        message, capped = _bounded(fallback.last_message, max_chars)
        truncated = fallback.truncated or capped
    return Extraction(app, "codex_desktop", status, message, len("\n".join(lines)), truncated, _bound_details(details))
