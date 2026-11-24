import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core import auditor, classifier, database, pitch_generator

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_p1_m08_whatsapp_mobile_registry_no_double_counting():
    """Verify NANPA Mobile numbers without verified on-site widgets do not poison active WhatsApp, double count, or show WhatsApp channel."""
    cfg = load_config()

    # Lead with Mobile phone, but site has no WhatsApp widget
    lead = {
        "business_name": "Apex Roofing Solutions",
        "website_url": "https://apexroofing.com",
        "clean_phone": "+12145550199",
        "line_type": "Mobile"
    }

    audit_data = {
        "audit_completed": True,
        "has_website": True,
        "is_unreachable": False,
        "is_whatsapp_active": False,
        "whatsapp_url": "https://wa.me/12145550199",
        "whatsapp_source": "mobile_registry",
        "flaw_codes": ["broken_mobile"],
        "failed_major_test": 1
    }

    # 1. Verification helper returns False
    assert not classifier._has_verified_whatsapp(lead, audit_data)

    # 2. Priority score awards mobile phone bonus (1.5), but NOT whatsapp bonus (1.5)
    score = classifier.calculate_priority_score(lead, audit_data, cfg)
    # broken_mobile: 3.0 + has_mobile_phone_bonus: 1.5 = 4.5
    assert score == pytest.approx(4.5)

    # 3. Primary channel is Phone, not WhatsApp
    status, reason, prio = classifier.classify_lead(lead, audit_data, cfg)
    assert status == "outreach_ready"
    assert "Phone Available" in reason
    assert "WhatsApp" not in reason


def test_p1_m08_consecutive_audits_no_self_poisoning():
    """Verify that a lead with previous mobile_registry whatsapp_url does not self-poison is_whatsapp_active on subsequent audits."""
    lead_after_first_audit = {
        "id": 1,
        "business_name": "Apex Roofing",
        "website_url": "https://apexroofing.com",
        "clean_phone": "+12145550199",
        "line_type": "Mobile",
        "whatsapp_url": "https://wa.me/12145550199",  # previously populated
        "whatsapp_source": "mobile_registry"
    }

    # Second audit finds NO on-site widget
    wa_url, is_active, source = auditor.extract_whatsapp_info(
        [], "<html><body>No Widget</body></html>", lead_after_first_audit["clean_phone"], "Mobile"
    )
    assert not is_active
    assert source == "mobile_registry"

    # Simulate auditor logic
    existing_wa_src = lead_after_first_audit.get("whatsapp_source")
    verified_wa_url = wa_url if is_active else None
    if not verified_wa_url and lead_after_first_audit.get("whatsapp_url"):
        if existing_wa_src != "mobile_registry" and source != "mobile_registry":
            verified_wa_url = lead_after_first_audit.get("whatsapp_url")

    persisted_is_active = bool(is_active)  # strictly from on-site
    assert persisted_is_active is False
    assert verified_wa_url is None


def test_p1_m09_unreachable_dead_domain_tagging_and_pitches(tmp_path):
    """Verify navigation failure is tagged as dead_domain (not server_delay) and pitch_generator retains dead_domain pitches even after contacted."""
    test_db = str(tmp_path / "test_dead_domain.db")
    database.init_db(test_db)

    # 1. Pitch generator checks bool(audit.get('is_unreachable'))
    contacted_dead_lead = {
        "id": 99,
        "business_name": "Ghost Roofing Contractors LLC",
        "city": "Fort Worth",
        "clean_phone": "+18175550122",
        "website_url": "https://ghostroofingtx.com",
        "status": "contacted",
        "status_reason": "Contacted via whatsapp (Follow-up: 2026-11-26)",
        "audit": {
            "audit_completed": True,
            "has_website": True,
            "is_unreachable": True,
            "flaw_codes": ["dead_domain"]
        }
    }

    pitches = pitch_generator.generate_pitches(contacted_dead_lead)
    # Pitch generator must recognize it as dead domain, not fall back to generic / no-website
    assert pitches["recommended"] == "accusation_audit"
    assert pitches["recommended_slot"] == "slot_2"
    assert "site down" in pitches["approaches"]["naive_curiosity"]["text"].lower()
    assert "wouldn't load" in pitches["approaches"]["accusation_audit"]["text"].lower()


@pytest.mark.asyncio
async def test_p1_m09_audit_single_site_navigation_failure_dead_domain(tmp_path):
    """Verify audit_single_site treats page.goto network errors as dead_domain and updates DB."""
    test_db = str(tmp_path / "test_audit_err.db")
    database.init_db(test_db)

    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_dead_domain_test_123",
            "business_name": "Expired Roofing",
            "website_url": "https://expired-roofing-dns-fail.com",
            "clean_phone": "+12145550199"
        },
        db_path=test_db
    )
    lead = database.get_lead_by_id(lead_id, db_path=test_db)

    mock_context = MagicMock()
    mock_page = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.goto.side_effect = Exception("net::ERR_NAME_NOT_RESOLVED at https://expired-roofing-dns-fail.com")
    mock_page.is_closed.return_value = False

    cfg = load_config()
    cfg["app"]["db_path"] = test_db
    semaphore = asyncio.Semaphore(1)

    await auditor.audit_single_site(mock_context, lead, cfg, semaphore)

    updated_lead = database.get_lead_by_id(lead_id, db_path=test_db)
    audit_data = json.loads(updated_lead["audit_json"])

    assert audit_data["is_unreachable"] is True
    assert "dead_domain" in audit_data["flaw_codes"]
    assert "server_delay" not in audit_data["flaw_codes"]
    assert "Dead Domain" in audit_data["flaw_labels"][0]
    assert updated_lead["status"] == "outreach_ready"


def test_p1_m12_should_preserve_status():
    """Verify _should_preserve_status correctly preserves outreach/manual statuses and allows automated disqualifications to re-evaluate."""
    # Active pipeline statuses must always be preserved
    for st in ("contacted", "replied", "won", "lost", "deferred"):
        assert auditor._should_preserve_status({"status": st}) is True

    # Automated disqualifications should NOT be preserved (they should re-evaluate on --re-audit)
    auto_reasons = [
        "Healthy Website (No Conversion Opportunity)",
        "Dead Domain (No Contact)",
        "Missing Business Name"
    ]
    for r in auto_reasons:
        lead = {"status": "disqualified", "status_reason": r}
        assert auditor._should_preserve_status(lead) is False

    # Manually triaged disqualifications MUST be preserved
    assert auditor._should_preserve_status({
        "status": "disqualified",
        "status_reason": "Manually disqualified: Out of business"
    }) is True

    assert auditor._should_preserve_status({
        "status": "disqualified",
        "status_reason": "Healthy Website (No Conversion Opportunity)",
        "manual_notes": "[reviewed] operator verified competitor"
    }) is True

    assert auditor._should_preserve_status({
        "status": "disqualified",
        "status_reason": "Custom operator rejection reason"
    }) is True

    # Normal new lead
    assert auditor._should_preserve_status({"status": "new"}) is False
