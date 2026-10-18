"""Window image capture module using bundled PowerShell script."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import errors
from .errors import CuError

if TYPE_CHECKING:
    from .config import Config


def get_script_path() -> Path:
    """Return the absolute path to the bundled cu_capture.ps1 script."""
    p = Path(__file__).resolve().parent / "scripts" / "cu_capture.ps1"
    if p.is_file():
        return p
    try:
        from importlib import resources
        rp = Path(str(resources.files("mcp_computer_use").joinpath("scripts/cu_capture.ps1")))
        if rp.is_file():
            return rp
    except Exception:
        pass
    return p


def capture_window(title: str, cfg: Config | None = None) -> tuple[bytes, dict]:
    """Capture a screenshot of the specified window and return (jpeg_bytes, meta_dict).

    Args:
        title: Window title (must be non-empty, <= 300 chars).
        cfg: Optional configuration object containing image_timeout.

    Returns:
        tuple[bytes, dict]: Raw JPEG image bytes and metadata dict with keys
                            {"width": int, "height": int, "bytes": int}.

    Raises:
        CuError: If capture fails, window is minimized/not found, capture is blank, or times out.
    """
    if not isinstance(title, str) or not title.strip():
        raise CuError(errors.IMAGE_CAPTURE_FAILED, "Window title must be a non-empty string")
    if len(title) > 300:
        raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Window title exceeds 300 characters ({len(title)} chars)")

    script_path = get_script_path()
    if not script_path.is_file():
        raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Capture script not found: {script_path}")

    ps_bin = shutil.which("pwsh") or "powershell.exe"
    b64_title = base64.b64encode(title.encode("utf-8")).decode("ascii")

    fd, tmp_str = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    tmp_path = Path(tmp_str)

    timeout = getattr(cfg, "image_timeout", 15.0) if cfg is not None else 15.0
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    cmd = [
        ps_bin,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-TitleB64",
        b64_title,
        "-OutPath",
        str(tmp_path),
    ]

    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=creationflags,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CuError(errors.IMAGE_CAPTURE_TIMEOUT, f"Image capture timed out after {timeout}s")
        except Exception as exc:
            raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Failed to execute capture process: {exc}")

        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            err_detail = f": {proc.stderr.strip()}" if proc.stderr and proc.stderr.strip() else ""
            raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Capture script produced no output{err_detail}")

        try:
            data = json.loads(lines[-1])
        except Exception:
            raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Invalid JSON output from capture script: {proc.stdout.strip()}")

        if not isinstance(data, dict):
            raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Expected JSON object from capture script, got {type(data).__name__}")

        if not data.get("ok"):
            code = data.get("code", errors.IMAGE_CAPTURE_FAILED)
            msg = data.get("message", "Image capture failed")
            raise CuError(code, msg)

        if not tmp_path.is_file():
            raise CuError(errors.IMAGE_CAPTURE_FAILED, "Captured image file was not created")

        raw_bytes = tmp_path.read_bytes()
        if len(raw_bytes) == 0:
            raise CuError(errors.IMAGE_CAPTURE_FAILED, "Captured image file is empty")

        if len(raw_bytes) < 2 or raw_bytes[:2] != b"\xff\xd8":
            raise CuError(errors.IMAGE_CAPTURE_FAILED, "Captured image is not a valid JPEG")

        try:
            width = int(data["width"])
            height = int(data["height"])
            _ = int(data.get("bytes", len(raw_bytes)))
        except (KeyError, ValueError, TypeError) as exc:
            raise CuError(errors.IMAGE_CAPTURE_FAILED, f"Invalid dimension or byte count in capture metadata: {exc}")

        meta = {
            "width": width,
            "height": height,
            "bytes": len(raw_bytes),
        }
        return raw_bytes, meta

    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
