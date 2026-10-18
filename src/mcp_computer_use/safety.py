"""Safety policies, action blocklist, and action auditing."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .sessions import sessions_dir

DEFAULT_BLOCKLIST: tuple[str, ...] = (
    # English dangerous actions
    "Delete",
    "Remove",
    "Send",
    "Allow",
    "Approve",
    "Accept",
    "Pay",
    "Buy",
    "Purchase",
    "Submit",
    "Confirm",
    "Publish",
    "Merge",
    "Uninstall",
    "Format",
    # Hebrew multi-word (placed first so phrase matches before single word)
    "הסר התקנה",
    # Hebrew base actions
    "מחק",
    "הסר",
    "שלח",
    "אשר",
    "אפשר",
    "שלם",
    "קנה",
    "פרסם",
    # Hebrew plural imperatives
    "מחקו",
    "הסירו",
    "שלחו",
    "אשרו",
    "אפשרו",
    "שלמו",
    "קנו",
    "פרסמו",
    # Hebrew inflected forms
    "מחיקה",
    "למחוק",
    "הסרה",
    "להסיר",
    "שליחה",
    "לשלוח",
    "אישור",
    "לאשר",
    "לאפשר",
    "תשלום",
    "לשלם",
    "קנייה",
    "לקנות",
    "פרסום",
    "לפרסם",
)

HEBREW_PREFIXES = frozenset("והבלמשכ")

WHITELISTED_LOG_FIELDS = frozenset({
    "time",
    "client",
    "session_id",
    "app",
    "action",
    "target",
    "ref",
    "name",
    "role",
    "result",
    "confirmed",
    "error_code",
    "text_len",
    "hint",
})

ALLOWED_ACTIONS = frozenset({"click", "type"})
ALLOWED_RESULTS = frozenset({"done", "blocked", "ambiguous", "not_found", "error"})

_LOG_LOCK = threading.Lock()
_STATE = threading.local()


def blocklist(cfg: Config | None = None) -> list[str]:
    """Return effective blocklist: defaults ∪ config additions (never fewer than defaults)."""
    seen: set[str] = set()
    result: list[str] = []

    def _add(entry: str) -> None:
        s = entry.strip()
        if not s:
            return
        key = s.casefold()
        if key not in seen:
            seen.add(key)
            result.append(s)

    for item in DEFAULT_BLOCKLIST:
        _add(item)

    if cfg is not None:
        raw_bl = getattr(cfg, "safety_blocklist", "")
        if isinstance(raw_bl, (list, tuple, set)):
            for x in raw_bl:
                _add(str(x))
        elif raw_bl:
            for x in str(raw_bl).split(","):
                _add(x)

        raw_extra = getattr(cfg, "safety_blocklist_extra", "")
        if isinstance(raw_extra, (list, tuple, set)):
            for x in raw_extra:
                _add(str(x))
        elif raw_extra:
            for x in str(raw_extra).split(","):
                _add(x)

    return result


def _normalize_tokens(text: str) -> list[str]:
    # 1. NFKC normalization
    s = unicodedata.normalize("NFKC", text)
    # 2. Hebrew maqaf U+05BE -> space
    s = s.replace("\u05be", " ")
    # 3. Strip Hebrew niqqud / cantillation U+0591-U+05C7
    s = re.sub(r"[\u0591-\u05c7]", "", s)
    # 4. Strip ALL Unicode format characters (category Cf, e.g. U+2060, U+200B-U+200F, U+FEFF)
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    # 5. Casefold for case-insensitive Latin
    s = s.casefold()
    # 6. Split on \W+ (Unicode non-alphanumeric)
    return [t for t in re.split(r"\W+", s, flags=re.UNICODE) if t]


def _is_hebrew(s: str) -> bool:
    return any("\u0590" <= c <= "\u05ff" for c in s)


def _is_latin(s: str) -> bool:
    return any("a" <= c <= "z" for c in s)


def _token_matches(text_token: str, entry_token: str) -> bool:
    if text_token == entry_token:
        return True
    # Latin prefix: text starts with entry and is <= 4 extra chars
    if _is_latin(entry_token) and _is_latin(text_token):
        if text_token.startswith(entry_token) and 0 < len(text_token) - len(entry_token) <= 4:
            return True
    # Hebrew prefix: single prefix letter from והבלמשכ
    if _is_hebrew(entry_token) and _is_hebrew(text_token):
        if (
            len(text_token) == len(entry_token) + 1
            and text_token[0] in HEBREW_PREFIXES
            and text_token[1:] == entry_token
        ):
            return True
    return False


def _match_text(text: str | None, words: Sequence[str]) -> str | None:
    if not text:
        return None
    text_tokens = _normalize_tokens(text)
    if not text_tokens:
        return None

    parsed_words = []
    for idx, w in enumerate(words):
        wtoks = _normalize_tokens(w)
        if wtoks:
            parsed_words.append((len(wtoks), idx, w, wtoks))
    # Prefer longer token matches first, preserving order among equal lengths
    parsed_words.sort(key=lambda item: (-item[0], item[1]))

    for _, _, orig_word, entry_tokens in parsed_words:
        m = len(entry_tokens)
        n = len(text_tokens)
        if m > n:
            continue
        for i in range(n - m + 1):
            if all(_token_matches(text_tokens[i + j], entry_tokens[j]) for j in range(m)):
                return orig_word
    return None


def is_dangerous(
    target: str | None,
    element_name: str | None,
    words: Sequence[str],
) -> str | None:
    """Check if target or element_name matches any blocked rule.
    
    Returns the matched entry string, or None if safe.
    Target is checked first, then element_name.
    Callers must pass `words` (e.g. `blocklist(cfg)`).
    """
    # Check target first (before the snapshot)
    matched = _match_text(target, words)
    if matched is not None:
        return matched
    # Check element_name second
    return _match_text(element_name, words)


def _safe_ttl(val: Any) -> float:
    """Clamp TTL to (0, 87600] hours (up to 10 years); fallback to 24h on invalid values."""
    try:
        if val is None:
            return 24.0
        f = float(val)
        if math.isnan(f) or math.isinf(f) or f <= 0:
            return 24.0
        if f > 87600.0:
            return 87600.0
        return f
    except (ValueError, TypeError, OverflowError):
        return 24.0


def purge_log(
    path: str | Path,
    ttl_hours: float,
    now: datetime | float | int | str | None = None,
) -> int:
    """Purge lines older than now - ttl_hours and unparsable lines.
    
    Rewrites atomically via temporary file only if something was dropped.
    Never raises an exception (catches read/replace errors, returns 0).
    Returns the count of dropped lines.
    """
    _STATE.purge_error = None
    try:
        log_file = Path(path)
        if not log_file.is_file():
            return 0

        ttl_hours = _safe_ttl(ttl_hours)

        try:
            if now is None:
                now_dt = datetime.now(timezone.utc)
            elif isinstance(now, datetime):
                now_dt = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            elif isinstance(now, (int, float)):
                now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
            else:
                s = str(now).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                now_dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except Exception:
            now_dt = datetime.now(timezone.utc)

        cutoff_dt = now_dt - timedelta(hours=ttl_hours)

        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            _STATE.purge_error = e
            return 0

        # Split on "\n" only (not splitlines to preserve U+2028 / U+2029)
        raw_lines = content.split("\n")
        kept: list[str] = []
        dropped = 0

        for raw_line in raw_lines:
            line = raw_line.rstrip("\r")
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    dropped += 1
                    continue
                time_val = data.get("time")
                if not time_val:
                    dropped += 1
                    continue
                s = str(time_val).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff_dt:
                    dropped += 1
                else:
                    kept.append(line)
            except Exception:
                dropped += 1

        if dropped > 0:
            tmp_path = log_file.with_name(f"{log_file.name}.{os.getpid()}_{time.time_ns()}.tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for item in kept:
                        f.write(item + "\n")
                os.replace(tmp_path, log_file)
            except Exception as e:
                _STATE.purge_error = e
                return 0
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

        return dropped
    except Exception as e:
        _STATE.purge_error = e
        return 0


def _acquire_file_lock(lock_fd: int, timeout: float = 5.0) -> bool:
    if timeout <= 0:
        return False
    end_time = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= end_time:
                return False
            time.sleep(0.005)


def _release_file_lock(lock_fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _build_needles(text: str) -> set[str]:
    """Collect the text and all >= 6-char fragments of raw and JSON-escaped forms."""
    needles: set[str] = set()
    if not text:
        return needles

    # 1. Raw text
    needles.add(text)
    if len(text) >= 6:
        for i in range(len(text) - 5):
            needles.add(text[i : i + 6])

    # 2. ensure_ascii=False JSON-escaped form
    try:
        esc_false = json.dumps(text, ensure_ascii=False)[1:-1]
        needles.add(esc_false)
        if len(esc_false) >= 6:
            for i in range(len(esc_false) - 5):
                needles.add(esc_false[i : i + 6])
    except Exception:
        pass

    # 3. ensure_ascii=True JSON-escaped form
    try:
        esc_true = json.dumps(text, ensure_ascii=True)[1:-1]
        needles.add(esc_true)
        if len(esc_true) >= 6:
            for i in range(len(esc_true) - 5):
                needles.add(esc_true[i : i + 6])
    except Exception:
        pass

    needles.discard("")
    return needles


def _scrub_value(val: str | None, needles: set[str]) -> str | None:
    """Redact value if raw value OR either serialized form contains any needle."""
    if val is None or not needles:
        return val
    cand_raw = str(val)
    candidates = [cand_raw]
    try:
        candidates.append(json.dumps(cand_raw, ensure_ascii=False)[1:-1])
    except Exception:
        pass
    try:
        candidates.append(json.dumps(cand_raw, ensure_ascii=True)[1:-1])
    except Exception:
        pass

    for needle in needles:
        for cand in candidates:
            if needle in cand:
                return "<redacted>"
    return val


def log_action(
    cfg: Config | None = None,
    text: str | None = None,
    **fields,
) -> str | None:
    """Append one JSON action audit record to sessions_dir(cfg)/actions.log.
    
    Accepts ONLY whitelisted fields. If `text` is passed (non-empty), it is used
    ONLY for scrubbing (never written): any logged string field whose raw or
    serialized form contains `text` or any >= 6-char fragment of the text's
    raw or serialized forms is replaced with '<redacted>'.
    `text_len` is derived from `len(text)`.
    
    Serializes purge + append across threads and processes using threading.Lock
    and actions.log.lock file lock within a shared 5s deadline. On timeout of
    either, does NOT append and returns 'action log skipped: lock timeout'.
    Never raises an exception for any reason, returning a hint on error.
    """
    try:
        cfg = Config() if cfg is None else cfg
        folder = sessions_dir(cfg)
        folder.mkdir(parents=True, exist_ok=True)
        log_file = folder / "actions.log"
        lock_file = folder / "actions.log.lock"

        # Resolve text for scrubbing
        raw_text = text if text is not None else fields.get("text")
        text_val = str(raw_text) if raw_text is not None and str(raw_text) else None
        needles = _build_needles(text_val) if text_val else set()

        # time: ALWAYS generated internally (caller-supplied time ignored)
        time_str = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # action: must be one of {"click", "type"}, else "other"
        raw_action = str(fields.get("action")) if fields.get("action") is not None else ""
        action = raw_action if raw_action in ALLOWED_ACTIONS else "other"

        # result: must be one of {"done", "blocked", "ambiguous", "not_found", "error"}, else "other"
        raw_result = str(fields.get("result")) if fields.get("result") is not None else ""
        result = raw_result if raw_result in ALLOWED_RESULTS else "other"

        # confirmed: bool
        confirmed = bool(fields.get("confirmed", False))

        # text_len: derived from text if provided, else non-negative int from fields, else None
        if text_val is not None:
            text_len = len(text_val)
        else:
            raw_text_len = fields.get("text_len")
            if isinstance(raw_text_len, int) and not isinstance(raw_text_len, bool) and raw_text_len >= 0:
                text_len = raw_text_len
            elif isinstance(raw_text_len, (str, float)):
                try:
                    parsed_len = int(raw_text_len)
                    text_len = parsed_len if parsed_len >= 0 else None
                except (ValueError, TypeError, OverflowError):
                    text_len = None
            else:
                text_len = None

        # All remaining string fields are scrubbed
        target_val = fields.get("target")
        target = str(target_val)[:100] if target_val is not None else None

        name_val = fields.get("name")
        if name_val is not None:
            name = str(name_val)[:80] if action == "type" else str(name_val)
        else:
            name = None

        def _to_str_or_none(k: str) -> str | None:
            v = fields.get(k)
            return str(v) if v is not None else None

        str_values = {
            "client": _to_str_or_none("client"),
            "session_id": _to_str_or_none("session_id"),
            "app": _to_str_or_none("app"),
            "target": target,
            "ref": _to_str_or_none("ref"),
            "name": name,
            "role": _to_str_or_none("role"),
            "error_code": _to_str_or_none("error_code"),
            "hint": _to_str_or_none("hint"),
        }

        if needles:
            for k in str_values:
                str_values[k] = _scrub_value(str_values[k], needles)

        # Build final record with ONLY whitelisted fields
        record = {
            "time": time_str,
            "client": str_values["client"],
            "session_id": str_values["session_id"],
            "app": str_values["app"],
            "action": action,
            "target": str_values["target"],
            "ref": str_values["ref"],
            "name": str_values["name"],
            "role": str_values["role"],
            "result": result,
            "confirmed": confirmed,
            "error_code": str_values["error_code"],
            "text_len": text_len,
            "hint": str_values["hint"],
        }

        line = json.dumps(record, ensure_ascii=False) + "\n"

        # Shared 5-second deadline across thread lock and OS lock
        start_time = time.monotonic()
        deadline = start_time + 5.0

        thread_locked = _LOG_LOCK.acquire(timeout=5.0)
        if not thread_locked:
            return "action log skipped: lock timeout"

        acquired_os = False
        lock_fd = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "action log skipped: lock timeout"

            try:
                lock_fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
                acquired_os = _acquire_file_lock(lock_fd, timeout=remaining)
            except OSError:
                acquired_os = False

            if not acquired_os:
                return "action log skipped: lock timeout"

            # Purge with error reporting
            purge_hint = None
            ttl = getattr(cfg, "action_log_ttl_hours", 24.0)
            purge_log(log_file, ttl_hours=ttl)
            purge_err = getattr(_STATE, "purge_error", None)
            if purge_err is not None:
                purge_hint = f"action log purge failed: {purge_err.__class__.__name__}"

            # Append line under lock
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)

            if purge_hint is not None:
                return purge_hint
            return None
        finally:
            if acquired_os and lock_fd is not None:
                _release_file_lock(lock_fd)
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            _LOG_LOCK.release()
    except Exception as e:
        return f"Warning: could not write action log: {e.__class__.__name__}"
