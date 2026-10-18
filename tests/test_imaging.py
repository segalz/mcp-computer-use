"""Tests for window image capture module (imaging.py)."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_computer_use import errors
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.errors import CuError
from mcp_computer_use.imaging import capture_window, get_script_path


def _make_dummy_jpeg(path_str: str, width: int = 800, height: int = 600) -> bytes:
    p = Path(path_str)
    raw = b"\xff\xd8\xff\xe0" + b"\x00" * 60
    p.write_bytes(raw)
    return raw


def test_package_data_and_script_exists():
    """Verify cu_capture.ps1 exists on disk and is included in pyproject.toml."""
    script_path = get_script_path()
    assert script_path.is_file(), f"Script does not exist at {script_path}"
    assert script_path.name == "cu_capture.ps1"

    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "scripts/*.ps1" in pyproject_text, "scripts/*.ps1 not found in pyproject.toml package-data"


def test_error_codes_registered():
    """Verify all four image capture error codes exist and have defaults."""
    expected_codes = [
        errors.IMAGE_CAPTURE_FAILED,
        errors.IMAGE_CAPTURE_BLANK,
        errors.IMAGE_WINDOW_MINIMIZED,
        errors.IMAGE_CAPTURE_TIMEOUT,
    ]
    for code in expected_codes:
        payload = errors.error_payload(code, "test message")
        err = payload["error"]
        assert err["code"] == code
        assert err["retryable"] is True
        assert isinstance(err["suggestion"], str) and len(err["suggestion"]) > 0


def test_config_image_timeout():
    """Verify image_timeout configuration and env override."""
    cfg = Config()
    assert cfg.image_timeout == 15.0

    loaded = load_config(env={"MCP_CU_IMAGE_TIMEOUT": "30.0"})
    assert loaded.image_timeout == 30.0


def test_capture_window_title_validation():
    """Verify window title validation (non-empty, <= 300 chars)."""
    cfg = Config()

    with pytest.raises(CuError) as exc_info:
        capture_window("", cfg)
    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED

    with pytest.raises(CuError) as exc_info:
        capture_window("   ", cfg)
    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED

    too_long = "A" * 301
    with pytest.raises(CuError) as exc_info:
        capture_window(too_long, cfg)
    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED


def test_capture_window_argv_shape():
    """Verify the exact argv passed to subprocess.run."""
    cfg = Config(image_timeout=12.5)
    captured_cmd = []
    created_tmp = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        tmp_file = cmd[-1]
        created_tmp.append(tmp_file)
        _make_dummy_jpeg(tmp_file, 800, 600)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": True, "width": 800, "height": 600, "bytes": 64}),
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        with patch("shutil.which", return_value="C:\\pwsh.exe"):
            raw, meta = capture_window("Calculator", cfg)

    assert mock_run.called
    assert raw[:2] == b"\xff\xd8"
    assert meta == {"width": 800, "height": 600, "bytes": len(raw)}

    assert captured_cmd[0] == "C:\\pwsh.exe"
    assert captured_cmd[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    script_arg = Path(captured_cmd[6])
    assert script_arg.is_file()
    assert script_arg.name == "cu_capture.ps1"
    assert captured_cmd[7] == "-TitleB64"
    assert base64.b64decode(captured_cmd[8]).decode("utf-8") == "Calculator"
    assert captured_cmd[9] == "-OutPath"
    assert captured_cmd[10].endswith(".jpg")

    assert not Path(created_tmp[0]).exists()


@pytest.mark.parametrize("title", [
    'Window with "quotes" and \'single\'',
    '-Path "C:\\evil\\dir" -OtherParam',
    'מחשבון - כותרת בעברית',
    'Window [*?] with glob pattern [a-z]',
    '   Leading space window   ',
    'Title with special chars !@#$%^&*()_+~`',
])
def test_capture_window_special_titles_base64_safe(title):
    """Verify titles with quotes, leading dashes, Hebrew, globs are safely transported via base64."""
    cfg = Config()

    def fake_run(cmd, **kwargs):
        tmp_file = cmd[-1]
        _make_dummy_jpeg(tmp_file)
        b64_arg = cmd[8]
        decoded = base64.b64decode(b64_arg).decode("utf-8")
        assert decoded == title
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": True, "width": 100, "height": 100, "bytes": 64}),
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        raw, meta = capture_window(title, cfg)
        assert len(raw) > 0


def test_capture_window_pwsh_first_and_fallback():
    """Verify pwsh is preferred when available, falling back to powershell.exe."""
    cfg = Config()

    def fake_run(cmd, **kwargs):
        _make_dummy_jpeg(cmd[-1])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"ok":true,"width":1,"height":1}')

    # Case 1: pwsh available
    with patch("shutil.which", side_effect=lambda x: "C:\\bin\\pwsh.exe" if x == "pwsh" else None):
        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            capture_window("Title", cfg)
            assert mock_run.call_args[0][0][0] == "C:\\bin\\pwsh.exe"

    # Case 2: pwsh not available, powershell.exe fallback
    with patch("shutil.which", return_value=None):
        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            capture_window("Title", cfg)
            assert mock_run.call_args[0][0][0] == "powershell.exe"


def test_capture_window_create_no_window():
    """Verify creationflags includes CREATE_NO_WINDOW."""
    cfg = Config()

    def fake_run(cmd, **kwargs):
        _make_dummy_jpeg(cmd[-1])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"ok":true,"width":1,"height":1}')

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        capture_window("Title", cfg)
        flags = mock_run.call_args[1].get("creationflags", 0)
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert flags == expected


def test_capture_window_timeout():
    """Verify TimeoutExpired raises IMAGE_CAPTURE_TIMEOUT and removes temp file."""
    cfg = Config(image_timeout=7.0)
    created_tmp = []

    def fake_run(cmd, **kwargs):
        tmp_file = cmd[-1]
        created_tmp.append(tmp_file)
        # File might or might not have been created before timeout
        Path(tmp_file).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 7.0))

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        with pytest.raises(CuError) as exc_info:
            capture_window("Title", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_TIMEOUT
    assert "7.0s" in str(exc_info.value)
    assert mock_run.call_args[1]["timeout"] == 7.0
    assert not Path(created_tmp[0]).exists()


@pytest.mark.parametrize("error_code, msg", [
    (errors.IMAGE_WINDOW_MINIMIZED, "Target window is minimized"),
    (errors.IMAGE_CAPTURE_BLANK, "Captured image is blank"),
    (errors.IMAGE_CAPTURE_FAILED, "Window not found: Foo"),
])
def test_capture_window_mapped_error_codes(error_code, msg):
    """Verify ok: false responses map to the corresponding CuError codes and clean up temp file."""
    cfg = Config()
    created_tmp = []

    def fake_run(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": False, "code": error_code, "message": msg}),
        )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == error_code
    assert msg in str(exc_info.value)
    assert not Path(created_tmp[0]).exists()


def test_capture_window_non_json_output():
    """Verify non-JSON stdout raises IMAGE_CAPTURE_FAILED and cleans up temp file."""
    cfg = Config()
    created_tmp = []

    def fake_run(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="PowerShell syntax error: unexpected token",
            stderr="Error details",
        )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert "Invalid JSON" in str(exc_info.value)
    assert not Path(created_tmp[0]).exists()


def test_capture_window_empty_output():
    """Verify completely empty stdout raises IMAGE_CAPTURE_FAILED and cleans up temp file."""
    cfg = Config()
    created_tmp = []

    def fake_run(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr="Some warning",
        )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert not Path(created_tmp[0]).exists()


def test_capture_window_invalid_jpeg_file():
    """Verify invalid JPEG or empty file raises IMAGE_CAPTURE_FAILED and cleans up temp file."""
    cfg = Config()
    created_tmp = []

    def fake_run_png(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        Path(tmp).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": True, "width": 10, "height": 10}),
        )

    with patch("subprocess.run", side_effect=fake_run_png):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert "not a valid JPEG" in str(exc_info.value)
    assert not Path(created_tmp[0]).exists()

    def fake_run_empty(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        Path(tmp).write_bytes(b"")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": True, "width": 10, "height": 10}),
        )

    with patch("subprocess.run", side_effect=fake_run_empty):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert "file is empty" in str(exc_info.value)
    assert not Path(created_tmp[1]).exists()


def test_capture_window_file_not_created():
    """Verify missing output file raises IMAGE_CAPTURE_FAILED and cleans up."""
    cfg = Config()
    created_tmp = []

    def fake_run_no_file(cmd, **kwargs):
        tmp = cmd[-1]
        created_tmp.append(tmp)
        # Don't create the file, or delete it
        Path(tmp).unlink(missing_ok=True)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": True, "width": 10, "height": 10}),
        )

    with patch("subprocess.run", side_effect=fake_run_no_file):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert "was not created" in str(exc_info.value)


def test_capture_window_encoding_options():
    """Verify encoding='utf-8' and errors='replace' passed to subprocess.run."""
    cfg = Config()

    def fake_run(cmd, **kwargs):
        _make_dummy_jpeg(cmd[-1])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"ok":true,"width":1,"height":1}')

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        capture_window("Title", cfg)
        assert mock_run.call_args[1].get("encoding") == "utf-8"
        assert mock_run.call_args[1].get("errors") == "replace"


def test_capture_window_message_with_backslashes_and_tabs():
    """Verify messages with backslashes and tabs (e.g. file paths) parse cleanly."""
    cfg = Config()
    msg = "Cannot open C:\\Users\\user\\file.txt\twith\ttabs and \\backslashes\\"
    stdout_json = json.dumps({"ok": False, "code": errors.IMAGE_CAPTURE_FAILED, "message": msg})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout_json)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert exc_info.value.message == msg


@pytest.mark.parametrize("bad_meta", [
    {"ok": True, "width": "not-an-int", "height": 10},
    {"ok": True, "width": 10, "height": None},
    {"ok": True, "width": 10},  # missing height
    {"ok": True},  # missing width and height
])
def test_capture_window_bad_metadata_dimensions(bad_meta):
    """Verify non-int or missing dimensions raise IMAGE_CAPTURE_FAILED."""
    cfg = Config()

    def fake_run(cmd, **kwargs):
        _make_dummy_jpeg(cmd[-1])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(bad_meta))

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CuError) as exc_info:
            capture_window("Foo", cfg)

    assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
    assert "Invalid dimension" in str(exc_info.value)


def test_cu_capture_script_browser_aliases_and_ordinal_rules():
    """Verify cu_capture.ps1 defines required browser process aliases and ordinal comparison."""
    script_path = get_script_path()
    content = script_path.read_text(encoding="utf-8")

    # Browser process aliases must be present
    assert '"Google Chrome" = "chrome"' in content or "'Google Chrome' = 'chrome'" in content
    assert '"Microsoft Edge" = "msedge"' in content or "'Microsoft Edge' = 'msedge'" in content

    # Suffix check must use ordinal comparison, never wildcard/regex
    assert "EndsWith($suffix, [System.StringComparison]::Ordinal)" in content
    assert "IMAGE_CAPTURE_FAILED" in content
    assert "Ambiguous window" in content
    assert "Window not found" in content
    assert "StartsWith" not in content
    assert "$matches" not in content


def test_window_title_candidate_matching_pure_python():
    """Pure Python unit test validating the candidate matching rules from the spec."""
    def match_candidate(title: str, target_name: str) -> bool:
        clean = title.rstrip("\u200b\ufeff \t")
        suffix = " - " + target_name
        if clean.endswith(suffix):
            return True
        if clean == target_name:
            return True
        return False

    # Chrome titles
    assert match_candidate("GitHub - Google Chrome", "Google Chrome")
    assert match_candidate("New Tab - Google Chrome", "Google Chrome")
    assert match_candidate("Google Chrome", "Google Chrome")
    assert match_candidate("Google Chrome\u200b", "Google Chrome")

    # Edge titles with profile segment
    assert match_candidate("Page - Profile 1 - Microsoft Edge", "Microsoft Edge")
    assert match_candidate("Dashboard - Personal - Microsoft Edge", "Microsoft Edge")
    assert match_candidate("New Tab - Microsoft Edge", "Microsoft Edge")
    assert match_candidate("Microsoft Edge\u200b", "Microsoft Edge")

    # Negative matches
    assert not match_candidate("Microsoft Edge - Profile 1", "Microsoft Edge")
    assert not match_candidate("Calculator - notes.txt - Notepad", "Calculator")
    assert not match_candidate("Page - Firefox", "Google Chrome")
    assert not match_candidate("Notepad - Document", "Microsoft Edge")
    assert not match_candidate("Google Chrome Helper", "Google Chrome")


def test_capture_window_browser_app_error_messages():
    """Verify subprocess errors for browser app lookups map to CuError(IMAGE_CAPTURE_FAILED)."""
    cfg = Config()

    # Window not found
    def fake_not_found(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": False, "code": errors.IMAGE_CAPTURE_FAILED, "message": "Window not found: Google Chrome"}),
        )

    with patch("subprocess.run", side_effect=fake_not_found):
        with pytest.raises(CuError) as exc_info:
            capture_window("Google Chrome", cfg)
        assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
        assert "Window not found: Google Chrome" in exc_info.value.message

    # Ambiguous window
    def fake_ambiguous(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"ok": False, "code": errors.IMAGE_CAPTURE_FAILED, "message": "Ambiguous window for 'Google Chrome' (2 windows found)"}),
        )

    with patch("subprocess.run", side_effect=fake_ambiguous):
        with pytest.raises(CuError) as exc_info:
            capture_window("Google Chrome", cfg)
        assert exc_info.value.code == errors.IMAGE_CAPTURE_FAILED
        assert "Ambiguous window" in exc_info.value.message

