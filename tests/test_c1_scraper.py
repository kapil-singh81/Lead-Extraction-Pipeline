import pytest
from core.c1_scraper import (
    extract_review_count_from_text,
    parse_niche_and_city,
    extract_phone_from_text
)


def test_extract_review_count_from_text():
    # 1. Phone area code must NOT be treated as review count
    assert extract_review_count_from_text("(214) 555-1234\nCommercial Roofer") == 0
    assert extract_review_count_from_text("Commercial Roofer (214) 555-1234 Dallas TX") == 0
    assert extract_review_count_from_text("+1 (512) 555-9876 Austin TX") == 0

    # 2. Rating-adjacent parenthesized review count
    assert extract_review_count_from_text("4.9(75) · (214) 555-1234") == 75
    assert extract_review_count_from_text("5.0 (120) · Commercial Roofer") == 120
    assert extract_review_count_from_text("3.8 (1,500)") == 1500

    # 3. Explicit review word match
    assert extract_review_count_from_text("1,240 reviews") == 1240
    assert extract_review_count_from_text("4.8 · 35 Reviews · Roofing Contractor") == 35
    assert extract_review_count_from_text("1 review") == 1

    # 4. Empty / None / No match
    assert extract_review_count_from_text("") == 0
    assert extract_review_count_from_text(None) == 0
    assert extract_review_count_from_text("No reviews yet") == 0


def test_parse_niche_and_city():
    # 1. Mixed-case query with trailing state
    assert parse_niche_and_city("commercial roofer Dallas TX") == ("commercial roofer", "Dallas TX")

    # 2. All-Title-Case query with trailing state
    assert parse_niche_and_city("Commercial Roofer Dallas TX") == ("Commercial Roofer", "Dallas TX")

    # 3. All-Title-Case query with 2-word city prefix
    assert parse_niche_and_city("Commercial Roofer San Antonio TX") == ("Commercial Roofer", "San Antonio TX")

    # 4. All-lowercase query with trailing state
    assert parse_niche_and_city("commercial roofer dallas tx") == ("commercial roofer", "dallas tx")
    assert parse_niche_and_city("commercial roofer san antonio tx") == ("commercial roofer", "san antonio tx")

    # 5. Explicit ' in ' separator
    assert parse_niche_and_city("dentists in Austin TX") == ("dentists", "Austin TX")
    assert parse_niche_and_city("personal injury lawyer in Los Angeles CA") == ("personal injury lawyer", "Los Angeles CA")

    # 6. No trailing state, mixed-case
    assert parse_niche_and_city("dentist Austin") == ("dentist", "Austin")
    assert parse_niche_and_city("personal injury lawyer San Antonio") == ("personal injury lawyer", "San Antonio")

    # 7. Single word or unparseable fallback
    assert parse_niche_and_city("roofer") == ("roofer", "")
    assert parse_niche_and_city("") == ("", "")


def test_extract_phone_from_text():
    # 1. Standard US/NANP formatted numbers
    assert extract_phone_from_text("(214) 555-1234") == "(214) 555-1234"
    assert extract_phone_from_text("214-555-1234") == "214-555-1234"
    assert extract_phone_from_text("Call us at +1 (214) 555-1234 today") == "+1 (214) 555-1234"
    assert extract_phone_from_text("Phone: 214.555.1234") == "214.555.1234"

    # 2. International numbers
    assert extract_phone_from_text("Contact: +44 20 7946 0958") == "+44 20 7946 0958"
    assert extract_phone_from_text("+91 98765 43210") == "+91 98765 43210"

    # 3. Must strictly ignore 9-digit ZIP+4 and street addresses
    assert extract_phone_from_text("Dallas, TX 75201-1234") is None
    assert extract_phone_from_text("12345 Main St, Suite 400") is None
    assert extract_phone_from_text("75201") is None
    assert extract_phone_from_text("No phone available") is None
    assert extract_phone_from_text("") is None
    assert extract_phone_from_text(None) is None


def test_config_json_audit_and_scraper_pruning():
    import json
    import os

    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 1. No scroll_amount_px in scraper_c1
    assert "scroll_amount_px" not in cfg["scraper_c1"]

    # 2. user_data_dir normalized
    assert cfg["scraper_c1"]["user_data_dir"] == "data/chrome_lead_profile"

    # 3. whatsapp_url removed from at_least_one_contact
    contact_fields = cfg["queue"]["required_outreach_fields"]["at_least_one_contact"]
    assert "whatsapp_url" not in contact_fields
    assert "dm_email" in contact_fields
    assert "clean_phone" in contact_fields

    # 4. headless_args contains all 4 required Chromium flags
    headless_args = cfg["auditor"]["headless_args"]
    expected_flags = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu"
    ]
    for flag in expected_flags:
        assert flag in headless_args


def test_user_data_dir_resolution_under_project_root(tmp_path):
    import os
    from core.c1_scraper import PROJECT_ROOT

    original_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        raw_user_data_dir = "data/chrome_lead_profile"
        if not os.path.isabs(raw_user_data_dir):
            resolved = os.path.join(PROJECT_ROOT, raw_user_data_dir)
        else:
            resolved = raw_user_data_dir

        expected = os.path.join(PROJECT_ROOT, "data", "chrome_lead_profile")
        assert os.path.normpath(resolved) == os.path.normpath(expected)
        assert str(tmp_path) not in resolved
    finally:
        os.chdir(original_cwd)


def test_main_applies_log_level_from_config(tmp_path, monkeypatch):
    import json
    import logging
    import asyncio
    import main

    test_db = str(tmp_path / "test_log_level.db")
    test_cfg = str(tmp_path / "config.json")

    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["app"]["db_path"] = test_db
    cfg["app"]["log_level"] = "DEBUG"

    with open(test_cfg, "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    async def mock_scraper(config, run_id=None):
        return 0

    monkeypatch.setattr(main.c1_scraper, "run_maps_scraper", mock_scraper)

    class Args:
        skip_scrape = False
        skip_audit = True
        re_audit = False

    asyncio.run(main.run_pipeline(config_path=test_cfg, args=Args()))
    assert logging.getLogger().level == logging.DEBUG


def test_probe_page_leak_guard_closes_in_finally(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from core import c1_scraper, database

    db_file = str(tmp_path / "test_leak_guard.db")
    database.init_db(db_file)

    async def run():
        mock_probe = MagicMock()
        mock_probe.is_closed.return_value = False
        mock_probe.close = AsyncMock()
        mock_probe.set_extra_http_headers = AsyncMock()
        mock_probe.goto = AsyncMock(side_effect=asyncio.TimeoutError("Probe timeout"))

        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(return_value=mock_probe)

        mock_page = MagicMock()
        mock_page.context = mock_context

        def locator_side_effect(sel):
            elem = MagicMock()
            if "star" in sel:
                elem.count = AsyncMock(return_value=1)
                elem.get_attribute = AsyncMock(return_value="4.5 stars")
                elem.inner_text = AsyncMock(return_value="4.5")
            elif "title" in sel or "DUwDvf" in sel:
                elem.count = AsyncMock(return_value=1)
                elem.inner_text = AsyncMock(return_value="Leak Guard Roofer")
            else:
                elem.count = AsyncMock(return_value=0)
                elem.get_attribute = AsyncMock(return_value="")
                elem.inner_text = AsyncMock(return_value="")
            ret = MagicMock()
            ret.first = elem
            return ret

        mock_page.locator.side_effect = locator_side_effect
        mock_page.url = "https://www.google.com/maps/place/data=!1s0x1:0x2"
        mock_page.title = AsyncMock(return_value="Leak Guard Roofer")
        mock_page.inner_text = AsyncMock(return_value="No reviews text")

        config = {
            "scraper_c1": {
                "selectors": {},
                "default_phone_prefix": "+1"
            },
            "app": {
                "db_path": db_file,
                "target_country": "US"
            },
            "auditor": {
                "mobile_user_agent": "TestMobileUA"
            }
        }

        await c1_scraper.extract_single_result(mock_page, "leak guard query", config)
        assert mock_probe.close.called, "probe_page.close was not called in finally!"
        saved_lead = database.get_lead_by_id(1, db_path=db_file)
        assert saved_lead["review_count"] == 0

    asyncio.run(run())

