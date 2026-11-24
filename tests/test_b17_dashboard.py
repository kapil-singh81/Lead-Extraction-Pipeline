import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b17_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b17_test.db")
    database.init_db(test_db)

    # Use real config with isolated db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m03_line_type_and_status_dropdown_options(b17_client_and_db):
    """
    P3-M03: Verifies that line_type='Local (US)' and status='replied' render selected
    in both lead_card.html and lead_drawer.html, avoiding silent dropdown reset,
    and all 9 pipeline statuses plus custom line_type options are preserved.
    """
    client, db_path = b17_client_and_db

    audit_payload = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": [],
        "line_type": "Local (US)"
    }
    lead_id = database.insert_lead({
        "place_id": "ChIJ_b17_lead_01",
        "business_name": "Lone Star Restoration",
        "website_url": "https://lonestarrestore.com",
        "phone": "(214) 555-0188",
        "clean_phone": "2145550188",
        "line_type": "Local (US)",
        "status": "replied",
        "status_reason": "Contractor replied via email",
        "priority_score": 75.0,
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    # 1. Test lead_card.html
    card_resp = client.get(f"/api/leads/{lead_id}/card")
    assert card_resp.status_code == 200
    card_html = card_resp.text

    # Verify line_type selected
    assert '<option value="Local (US)" selected>Local (US)</option>' in card_html
    # Verify status selected
    assert '<option value="replied" selected>Replied</option>' in card_html
    # Verify all 9 pipeline statuses exist in lead_card.html
    for st_val in ['new', 'outreach_ready', 'contacted', 'replied', 'won', 'lost', 'missing_info', 'deferred', 'disqualified']:
        assert f'<option value="{st_val}"' in card_html

    # 2. Test lead_drawer.html
    drawer_resp = client.get(f"/api/leads/{lead_id}/modal")
    assert drawer_resp.status_code == 200
    drawer_html = drawer_resp.text

    assert '<option value="Local (US)" selected>Local (US)</option>' in drawer_html
    assert '<option value="replied" selected>Replied</option>' in drawer_html
    for st_val in ['new', 'outreach_ready', 'contacted', 'replied', 'won', 'lost', 'missing_info', 'deferred', 'disqualified']:
        assert f'<option value="{st_val}"' in drawer_html

    # 3. Test custom line_type fallback option
    custom_lead_id = database.insert_lead({
        "place_id": "ChIJ_b17_lead_02",
        "business_name": "Apex PBX Contractors",
        "website_url": "https://apexpbx.com",
        "line_type": "Custom PBX Virtual",
        "status": "won",
        "priority_score": 80.0
    }, db_path=db_path)

    custom_card = client.get(f"/api/leads/{custom_lead_id}/card").text
    assert '<option value="Custom PBX Virtual" selected>Custom PBX Virtual</option>' in custom_card
    assert '<option value="won" selected>Won</option>' in custom_card

    custom_drawer = client.get(f"/api/leads/{custom_lead_id}/modal").text
    assert '<option value="Custom PBX Virtual" selected>Custom PBX Virtual</option>' in custom_drawer
    assert '<option value="won" selected>Won</option>' in custom_drawer


def test_p3_m06_drawer_preselects_history_channel_status_outcome(b17_client_and_db):
    """
    P3-M06: Opening GET /api/leads/{id}/modal for a lead whose latest outreach_log
    has channel='email' and outcome='replied' pre-selects email, contacted, and replied
    in the drawer Status Manager.
    """
    client, db_path = b17_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b17_lead_03",
        "business_name": "Carter Law Partners",
        "website_url": "https://carterlaw.com",
        "dm_email": "carter@carterlaw.com",
        "status": "contacted",
        "status_reason": "Contacted via email",
        "priority_score": 82.0
    }, db_path=db_path)

    # Log outreach history with channel='email' and outcome='replied'
    database.log_outreach(
        lead_id=lead_id,
        channel="email",
        outcome="replied",
        message_sent="[reputation_dissonance] Pitch copy",
        notes="Client responded requesting meeting",
        db_path=db_path
    )

    resp = client.get(f"/api/leads/{lead_id}/modal")
    assert resp.status_code == 200
    html = resp.text

    # Channel select should pre-select email
    assert '<option value="email" selected>✉️ Cold Email</option>' in html
    # Pipeline status select (action) should pre-select contacted
    assert '<option value="contacted" selected>✓ Mark Contacted</option>' in html
    # Outcome select should pre-select replied
    assert '<option value="replied" selected>Replied / Interested</option>' in html


def test_p3_m06_drawer_infers_channel_from_contacts_when_no_history(b17_client_and_db):
    """
    P3-M06: Opening GET /api/leads/{id}/modal for a lead with no outreach_log
    and dm_email='a@b.com' (no WhatsApp) pre-selects email rather than hardcoding whatsapp.
    Also tests phone inference, and deferred status mapping to 'defer'.
    """
    client, db_path = b17_client_and_db

    # 1. Lead with email (no whatsapp)
    lead_email_id = database.insert_lead({
        "place_id": "ChIJ_b17_lead_04",
        "business_name": "Apex Dental Services",
        "website_url": "https://apexdental.com",
        "dm_email": "a@b.com",
        "status": "missing_info",
        "priority_score": 60.0
    }, db_path=db_path)

    resp_email = client.get(f"/api/leads/{lead_email_id}/modal")
    assert resp_email.status_code == 200
    html_email = resp_email.text

    # Channel should be inferred as email, NOT whatsapp
    assert '<option value="email" selected>✉️ Cold Email</option>' in html_email
    assert '<option value="whatsapp" selected>' not in html_email
    # Action should match missing_info
    assert '<option value="missing_info" selected>🔍 Move to Needs Research</option>' in html_email
    # Default outcome is sent
    assert '<option value="sent" selected>Sent / Initiated</option>' in html_email

    # 2. Lead with phone only (no email, no whatsapp) and deferred status
    lead_phone_id = database.insert_lead({
        "place_id": "ChIJ_b17_lead_05",
        "business_name": "Quick Fix Plumbing",
        "phone": "(214) 555-0999",
        "clean_phone": "2145550999",
        "status": "deferred",
        "priority_score": 50.0
    }, db_path=db_path)

    resp_phone = client.get(f"/api/leads/{lead_phone_id}/modal")
    assert resp_phone.status_code == 200
    html_phone = resp_phone.text

    assert '<option value="phone" selected>📞 Phone Call</option>' in html_phone
    assert '<option value="defer" selected>⏳ Defer 24 Hours</option>' in html_phone
