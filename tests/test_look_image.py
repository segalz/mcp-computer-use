"""Tests for optional window image in look_at_screen (Task C3)."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pydantic_core
import pytest
from mcp_types import ImageContent, TextContent

from mcp_computer_use import core, errors, guidance, imaging, server
from mcp_computer_use.config import Config
from mcp_computer_use.errors import CuError


@pytest.fixture
def cfg():
    return Config()


def test_golden_byte_identical_default(cfg):
    """Golden test: when include_image is false (or omitted), return is byte-identical single TextContent.

    structured_content must be None, tool output_schema must be None.
    """
    async def _run():
        sample_res = {
            "status": "ready",
            "summary": "Calculator is open and shows 42.",
            "windows_seen": ["Calculator"],
            "confidence": 0.95,
            "details": {"project": "demo"},
            "model_used": "gpt-oss:20b",
            "duration_seconds": 1.23,
        }

        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            mcp = server.build_server(cfg)
            tools = await mcp.list_tools()
            look_tool = next(t for t in tools if t.name == "look_at_screen")
            assert look_tool.output_schema is None

            # Call with default (omitted include_image)
            res_default = await mcp.call_tool("look_at_screen", {"question": "What is open?"})
            assert len(res_default.content) == 1
            assert isinstance(res_default.content[0], TextContent)
            assert res_default.structured_content is None

            # Expected serialization
            expected_bytes = pydantic_core.to_json(sample_res, fallback=str, indent=2)
            assert res_default.content[0].text.encode("utf-8") == expected_bytes

            # Call with explicit include_image=False
            res_false = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": False})
            assert len(res_false.content) == 1
            assert isinstance(res_false.content[0], TextContent)
            assert res_false.structured_content is None
            assert res_false.content[0].text.encode("utf-8") == expected_bytes

            # Neither image nor image_error should be present
            data = json.loads(res_default.content[0].text)
            assert "image" not in data
            assert "image_error" not in data

    asyncio.run(_run())


def test_look_include_image_true_returns_two_blocks(cfg):
    """When include_image is True, returns TextContent with image meta + ImageContent with base64 JPEG."""
    async def _run():
        sample_res = {
            "status": "ready",
            "summary": "Calculator is open",
            "windows_seen": ["Calculator"],
        }
        mock_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 30
        mock_meta = {"width": 800, "height": 600, "bytes": len(mock_jpeg)}

        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)) as mock_capture:
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})

                mock_capture.assert_called_once_with("Calculator", cfg)

                assert len(res.content) == 2
                block0 = res.content[0]
                block1 = res.content[1]

                assert isinstance(block0, TextContent)
                data0 = json.loads(block0.text)
                assert data0["summary"] == "Calculator is open"
                assert data0["image"] == mock_meta
                assert "image_error" not in data0

                assert isinstance(block1, ImageContent)
                assert block1.mime_type == "image/jpeg"
                assert base64.b64decode(block1.data) == mock_jpeg

    asyncio.run(_run())


def test_look_capture_cu_error_returns_one_block_with_image_error(cfg):
    """On CuError during capture, returns 1 block (TextContent) with image_error and text answer intact."""
    async def _run():
        sample_res = {
            "status": "ready",
            "summary": "Calculator is open",
            "windows_seen": ["Calculator"],
        }

        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            with patch.object(
                imaging,
                "capture_window",
                side_effect=CuError(errors.IMAGE_WINDOW_MINIMIZED, "Window is minimized"),
            ):
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})

                assert len(res.content) == 1
                block0 = res.content[0]
                assert isinstance(block0, TextContent)

                data0 = json.loads(block0.text)
                assert data0["summary"] == "Calculator is open"
                assert "image" not in data0
                assert "image_error" in data0

                img_err = data0["image_error"]
                assert img_err["code"] == errors.IMAGE_WINDOW_MINIMIZED
                assert img_err["message"] == "Window is minimized"
                assert img_err["retryable"] is True
                assert isinstance(img_err["suggestion"], str) and len(img_err["suggestion"]) > 0

    asyncio.run(_run())


def test_look_capture_unexpected_error_returns_image_error(cfg):
    """On unexpected exception during capture, returns 1 block with IMAGE_CAPTURE_FAILED."""
    async def _run():
        sample_res = {
            "status": "ready",
            "summary": "Calculator is open",
            "windows_seen": ["Calculator"],
        }

        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            with patch.object(imaging, "capture_window", side_effect=RuntimeError("Subprocess died")):
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})

                assert len(res.content) == 1
                data = json.loads(res.content[0].text)
                assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_FAILED
                assert "Subprocess died" in data["image_error"]["message"]

    asyncio.run(_run())


def test_look_error_skips_capture(cfg):
    """If core.look_at_screen itself fails, no capture is attempted and dict is returned directly."""
    async def _run():
        err_res = {
            "error": {
                "code": errors.APP_NOT_FOUND,
                "message": "App not found",
                "retryable": False,
                "suggestion": "Retry with explicit app",
            }
        }

        with patch.object(core, "look_at_screen", return_value=err_res.copy()):
            with patch.object(imaging, "capture_window") as mock_capture:
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})

                mock_capture.assert_not_called()
                assert len(res.content) == 1
                data = json.loads(res.content[0].text)
                assert "error" in data
                assert "image" not in data
                assert "image_error" not in data

    asyncio.run(_run())


def test_title_selection(cfg):
    """Verify window title resolution: windows_seen[0] first, fallback to app, error if none."""
    async def _run():
        mock_jpeg = b"\xff\xd8\xff\xe0JFIF"
        mock_meta = {"width": 100, "height": 100, "bytes": len(mock_jpeg)}

        # Case 1: windows_seen has multiple titles -> picks windows_seen[0]
        res1 = {"summary": "A", "windows_seen": ["SeenTitle", "OtherTitle"]}
        with patch.object(core, "look_at_screen", return_value=res1.copy()):
            with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)) as mock_cap:
                mcp = server.build_server(cfg)
                await mcp.call_tool("look_at_screen", {"question": "Q", "app": "AppArg", "include_image": True})
                mock_cap.assert_called_once_with("SeenTitle", cfg)

        # Case 2: windows_seen is empty -> falls back to app
        res2 = {"summary": "B", "windows_seen": []}
        with patch.object(core, "look_at_screen", return_value=res2.copy()):
            with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)) as mock_cap:
                mcp = server.build_server(cfg)
                await mcp.call_tool("look_at_screen", {"question": "Q", "app": "FallbackApp", "include_image": True})
                mock_cap.assert_called_once_with("FallbackApp", cfg)

        # Case 3: windows_seen is missing -> falls back to app
        res3 = {"summary": "C"}
        with patch.object(core, "look_at_screen", return_value=res3.copy()):
            with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)) as mock_cap:
                mcp = server.build_server(cfg)
                await mcp.call_tool("look_at_screen", {"question": "Q", "app": "FallbackApp2", "include_image": True})
                mock_cap.assert_called_once_with("FallbackApp2", cfg)

        # Case 4: neither windows_seen nor app -> image_error, no capture call
        res4 = {"summary": "D", "windows_seen": []}
        with patch.object(core, "look_at_screen", return_value=res4.copy()):
            with patch.object(imaging, "capture_window") as mock_cap:
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "Q", "include_image": True})
                mock_cap.assert_not_called()
                data = json.loads(res.content[0].text)
                assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_FAILED
                assert "No target window" in data["image_error"]["message"]

    asyncio.run(_run())


def test_guidance_descriptions_mention_thousands_of_tokens():
    """Verify IMAGE_DESCRIPTION and LOOK_DESCRIPTION mention 'thousands of tokens'."""
    assert "thousands of tokens" in guidance.IMAGE_DESCRIPTION
    assert "thousands of tokens" in guidance.LOOK_DESCRIPTION

    mcp = server.build_server(Config())
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    look = tools["look_at_screen"]
    schema = getattr(look, "input_schema", None) or getattr(look, "inputSchema")
    props = schema["properties"]
    assert "include_image" in props
    assert props["include_image"]["default"] is False
    assert "thousands of tokens" in props["include_image"]["description"]


def test_cli_image_out_success(tmp_path, capsys):
    """CLI --image-out PATH writes the captured JPEG and prints JSON with image metadata."""
    sample_res = {
        "status": "ready",
        "summary": "Calculator is open",
        "windows_seen": ["Calculator"],
    }
    mock_jpeg = b"\xff\xd8\xff\xe0mock_cli_jpeg"
    mock_meta = {"width": 640, "height": 480, "bytes": len(mock_jpeg)}
    out_file = tmp_path / "captured.jpg"

    with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
        with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)) as mock_cap:
            server.main(["--ask", "Is calc open?", "--image-out", str(out_file)])
            mock_cap.assert_called_once()

    assert out_file.is_file()
    assert out_file.read_bytes() == mock_jpeg

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["summary"] == "Calculator is open"
    assert data["image"] == mock_meta
    assert "image_error" not in data


def test_cli_image_out_capture_error(tmp_path, capsys):
    """CLI --image-out on capture error prints JSON with image_error and does not write file."""
    sample_res = {
        "status": "ready",
        "summary": "Calculator is open",
        "windows_seen": ["Calculator"],
    }
    out_file = tmp_path / "never_written.jpg"

    with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
        with patch.object(
            imaging,
            "capture_window",
            side_effect=CuError(errors.IMAGE_CAPTURE_BLANK, "Blank capture"),
        ):
            server.main(["--ask", "Is calc open?", "--image-out", str(out_file)])

    assert not out_file.exists()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["summary"] == "Calculator is open"
    assert "image" not in data
    assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_BLANK


def test_cli_image_out_look_error_skips_capture(tmp_path):
    """CLI --image-out when look itself fails skips capture and does not write file."""
    err_res = {
        "error": {
            "code": errors.APP_NOT_FOUND,
            "message": "App not found",
        }
    }
    out_file = tmp_path / "never_written.jpg"

    with patch.object(core, "look_at_screen", return_value=err_res.copy()):
        with patch.object(imaging, "capture_window") as mock_cap:
            server.main(["--ask", "Is calc open?", "--image-out", str(out_file)])
            mock_cap.assert_not_called()

    assert not out_file.exists()


def test_cli_image_out_without_ask_raises_argparse_error(capsys):
    """CLI --image-out without --ask raises SystemExit with code 2 and prints error."""
    with pytest.raises(SystemExit) as exc_info:
        server.main(["--image-out", "out.jpg"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--image-out requires --ask" in captured.err


def test_cli_image_out_file_write_failure(tmp_path, capsys):
    """CLI --image-out when file writing fails sets image_error with could not write image file: <ExceptionClass>."""
    sample_res = {
        "status": "ready",
        "summary": "Calculator is open",
        "windows_seen": ["Calculator"],
    }
    mock_jpeg = b"\xff\xd8\xff\xe0mock_cli_jpeg"
    mock_meta = {"width": 640, "height": 480, "bytes": len(mock_jpeg)}
    out_file = tmp_path / "protected.jpg"

    with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
        with patch.object(imaging, "capture_window", return_value=(mock_jpeg, mock_meta)):
            with patch.object(Path, "write_bytes", side_effect=PermissionError("Access denied")):
                server.main(["--ask", "Is calc open?", "--image-out", str(out_file)])

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["summary"] == "Calculator is open"
    assert "image" not in data
    assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_FAILED
    assert data["image_error"]["message"] == "could not write image file: PermissionError"


def test_look_browser_style_title_mismatch(cfg):
    """Browser app name (e.g. 'Google Chrome') in windows_seen maps to capture error if window not found or ambiguous."""
    async def _run():
        sample_res = {
            "status": "ready",
            "summary": "Browsing the web",
            "windows_seen": ["Google Chrome"],
        }
        # Case 1: Window not found
        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            with patch.object(
                imaging,
                "capture_window",
                side_effect=CuError(errors.IMAGE_CAPTURE_FAILED, "Window not found: Google Chrome"),
            ) as mock_cap:
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})
                mock_cap.assert_called_once_with("Google Chrome", cfg)
                assert len(res.content) == 1
                data = json.loads(res.content[0].text)
                assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_FAILED
                assert "Window not found: Google Chrome" in data["image_error"]["message"]

        # Case 2: Ambiguous window
        with patch.object(core, "look_at_screen", return_value=sample_res.copy()):
            with patch.object(
                imaging,
                "capture_window",
                side_effect=CuError(errors.IMAGE_CAPTURE_FAILED, "Ambiguous window for 'Google Chrome' (2 windows found)"),
            ) as mock_cap:
                mcp = server.build_server(cfg)
                res = await mcp.call_tool("look_at_screen", {"question": "What is open?", "include_image": True})
                mock_cap.assert_called_once_with("Google Chrome", cfg)
                assert len(res.content) == 1
                data = json.loads(res.content[0].text)
                assert data["image_error"]["code"] == errors.IMAGE_CAPTURE_FAILED
                assert "Ambiguous window" in data["image_error"]["message"]

    asyncio.run(_run())

