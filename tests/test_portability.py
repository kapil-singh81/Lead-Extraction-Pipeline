import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import auditor, c1_scraper, database
import dashboard

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_batch_scripts_portability_and_crlf():
    """Verify all .bat scripts use CRLF, anchor to %~dp0, and have no legacy broken refs."""
    bat_files = ["setup.bat", "start.bat", "dashboard.bat", "run.bat"]
    for name in bat_files:
        path = PROJECT_ROOT / name
        assert path.exists(), f"{name} must exist"
        raw = path.read_bytes()
        # Windows cmd.exe requires CRLF for reliable goto/call label seeking
        assert b"\r\n" in raw, f"{name} must use CRLF line endings"
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{name} has bare LF line endings"

        text = raw.decode("utf-8")
        assert 'pushd "%~dp0"' in text, f"{name} must anchor CWD to script directory"
        assert not re.search(r"(?<!from core )import\s+database\b", text), (
            f"{name} still uses broken top-level 'import database'"
        )
        assert "VENV_PY=" in text, f"{name} must resolve venv Python explicitly"
        for line in text.splitlines():
            if line.strip().lower().startswith("title "):
                assert " & " not in line, f"{name} has unescaped '&' in title line: {line}"


def test_posix_scripts_lf_endings():
    """Verify .sh scripts exist and use Unix LF line endings."""
    for name in ["setup.sh", "run.sh"]:
        path = PROJECT_ROOT / name
        assert path.exists(), f"{name} must exist"
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"{name} must use LF line endings, not CRLF"
        assert raw.startswith(b"#!/usr/bin/env bash\n")


def test_requirements_include_pytest_and_runtime_deps():
    """Verify requirements.txt includes both runtime and pytest test dependencies."""
    reqs = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for pkg in ("playwright", "fastapi", "uvicorn", "jinja2", "python-multipart", "httpx", "pytest", "pytest-asyncio"):
        assert pkg in reqs, f"Missing {pkg} in requirements.txt"


def test_c1_scraper_browser_channel_fallback():
    """Verify c1_scraper falls back to Playwright Chromium when Chrome is not installed."""
    async def run():
        mock_context = MagicMock()
        mock_chromium = MagicMock()
        mock_chromium.launch_persistent_context = AsyncMock(
            side_effect=[
                Exception("BrowserType.launch_persistent_context: Chromium distribution 'chrome' is not found"),
                mock_context,
            ]
        )
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium

        cfg = {
            "browser_channel": "chrome",
            "headless": True,
            "launch_args": ["--no-sandbox"],
            "viewport": {"width": 1280, "height": 720},
        }
        ctx = await c1_scraper._launch_persistent_browser(mock_playwright, "data/test_profile", cfg)
        assert ctx is mock_context
        assert mock_chromium.launch_persistent_context.call_count == 2
        first_call = mock_chromium.launch_persistent_context.call_args_list[0].kwargs
        second_call = mock_chromium.launch_persistent_context.call_args_list[1].kwargs
        assert first_call["channel"] == "chrome"
        assert "channel" not in second_call

    asyncio.run(run())


def test_c1_scraper_does_not_mask_profile_lock():
    """Verify profile-in-use lock errors are raised rather than hidden by channel fallback."""
    async def run():
        mock_chromium = MagicMock()
        mock_chromium.launch_persistent_context = AsyncMock(
            side_effect=Exception("ProcessSingleton: user data directory is already in use")
        )
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium

        with pytest.raises(Exception, match="already in use"):
            await c1_scraper._launch_persistent_browser(
                mock_playwright,
                "data/test_profile",
                {"browser_channel": "chrome", "headless": True},
            )
        assert mock_chromium.launch_persistent_context.call_count == 1

    asyncio.run(run())


def test_relative_config_resolution_from_foreign_cwd(tmp_path, monkeypatch):
    """Verify dashboard, scraper, and auditor resolve config.json relative to PROJECT_ROOT from any CWD."""
    orig_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        cfg = dashboard.load_config()
        assert cfg.get("app", {}).get("db_path") == "data/lead_pipeline.db"

        # Auditor run_site_auditor with relative "config.json" from foreign CWD
        temp_db = str(tmp_path / "foreign_cwd.db")
        database.init_db(temp_db)
        monkeypatch.setattr(database, "get_unaudited_leads", lambda *a, **kw: [])
        monkeypatch.setattr(auditor, "process_no_website_leads", lambda *a, **kw: 0)
        asyncio.run(auditor.run_site_auditor("config.json"))
    finally:
        os.chdir(orig_cwd)
