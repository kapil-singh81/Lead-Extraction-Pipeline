import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b22_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b22_test.db")
    database.init_db(test_db)

    # Use real config with isolated test db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m02_pitch_word_count_live_badge(b22_client_and_db):
    """
    P3-m02:
    GET /api/leads/{id}/card and GET /api/leads/{id}/modal render
    id="pitch-word-count-{id}" with the real word count (zero hardcoded < 45 Words text).
    """
    client, db_path = b22_client_and_db

    audit_payload = {
        "audit_completed": True,
        "flaw_codes": ["slow_load"],
        "flaw_labels": ["Slow Load Speed (3.2s)"],
        "metrics": {"load_time_ms": 3200}
    }

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b22_count_01",
        "business_name": "Apex Roofing Specialists",
        "clean_phone": "+12145553344",
        "city": "Dallas",
        "website_url": "https://apexroofingdfw.com",
        "rating": 4.7,
        "priority_score": 82.0,
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    # 1. Lead card endpoint
    resp_card = client.get(f"/api/leads/{lead_id}/card")
    assert resp_card.status_code == 200
    card_html = resp_card.text
    assert f'id="pitch-word-count-{lead_id}"' in card_html
    assert "words • Zero Links" in card_html
    assert "✓ &lt; 45 Words • Zero Links" not in card_html
    assert "✓ < 45 Words • Zero Links" not in card_html

    # 2. Drawer modal endpoint
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text
    assert f'id="pitch-word-count-drawer-{lead_id}"' in modal_html
    assert "words • Zero Links" in modal_html
    assert "✓ &lt; 45 Words • Zero Links" not in modal_html
    assert "✓ < 45 Words • Zero Links" not in modal_html


def test_p3_m03_manual_note_hint_and_whatsapp_fallback(b22_client_and_db):
    """
    P3-m03:
    A healthy lead (needs_manual_note=True) with a valid phone number renders
    the manual-note hint AND still renders Open WhatsApp ↗ pointing to https://wa.me/...
    """
    client, db_path = b22_client_and_db

    # Healthy website lead with no flaws
    audit_payload = {
        "audit_completed": True,
        "flaw_codes": [],
        "flaw_labels": []
    }

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b22_healthy_02",
        "business_name": "Pristine Dental Care",
        "phone": "(214) 555-7788",
        "clean_phone": "+12145557788",
        "city": "Dallas",
        "website_url": "https://pristinedental.com",
        "rating": 4.9,
        "review_count": 85,
        "priority_score": 70.0,
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    # 1. Lead card endpoint
    resp_card = client.get(f"/api/leads/{lead_id}/card")
    assert resp_card.status_code == 200
    card_html = resp_card.text
    assert "ℹ️ No automated flaw detected — write a custom note below before sending." in card_html
    assert "💬 Open WhatsApp ↗" in card_html
    assert "https://wa.me/12145557788" in card_html

    # 2. Drawer modal endpoint
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text
    assert "ℹ️ No automated flaw detected — write a custom note below before sending." in modal_html
    assert "💬 Open WhatsApp ↗" in modal_html
    assert "https://wa.me/12145557788" in modal_html


def test_p3_m06_youtube_and_dm_linkedin_external_url_and_rel_noopener(b22_client_and_db):
    """
    P3-m06:
    A lead with youtube_url="https://youtube.com/@apexroofing" and dm_linkedin="linkedin.com/in/owner"
    renders ▶️ YouTube ↗ and https://linkedin.com/in/owner with rel="noopener noreferrer".
    """
    client, db_path = b22_client_and_db

    audit_payload = {
        "audit_completed": True,
        "flaw_codes": ["slow_load"],
        "youtube_url": "https://youtube.com/@apexroofing"
    }

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b22_social_03",
        "business_name": "Apex Construction",
        "dm_name": "Marcus Vance",
        "dm_linkedin": "linkedin.com/in/owner",
        "website_url": "https://apexconstruction.com",
        "maps_url": "https://maps.google.com/?cid=12345",
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    # 1. Lead card
    resp_card = client.get(f"/api/leads/{lead_id}/card")
    assert resp_card.status_code == 200
    card_html = resp_card.text

    assert "▶️ YouTube ↗" in card_html
    assert 'href="https://youtube.com/@apexroofing"' in card_html
    assert 'href="https://linkedin.com/in/owner"' in card_html
    assert 'rel="noopener noreferrer"' in card_html

    # 2. Drawer modal
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text

    assert "▶️ YouTube ↗" in modal_html
    assert 'href="https://youtube.com/@apexroofing"' in modal_html
    assert 'href="https://linkedin.com/in/owner"' in modal_html
    assert 'rel="noopener noreferrer"' in modal_html


def test_p3_m08_hx_confirm_on_outreach_delete_and_badge_rec_visible(b22_client_and_db):
    """
    P3-m08:
    GET /api/leads/{id}/modal with outreach history includes hx-confirm= on .btn-delete-log,
    and static/style.css does not hide .badge-rec with display: none.
    """
    client, db_path = b22_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b22_history_04",
        "business_name": "Legacy Builders",
        "priority_score": 60.0
    }, db_path=db_path)

    database.log_outreach(
        lead_id=lead_id,
        channel="email",
        outcome="sent",
        message_sent="[loss_aversion] Hey Legacy Builders",
        db_path=db_path
    )

    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text

    assert 'hx-confirm="Delete this outreach log entry?"' in modal_html
    assert "btn-delete-log" in modal_html

    # Check static/style.css for .badge-rec
    css_content = Path("static/style.css").read_text(encoding="utf-8")
    assert ".badge-rec {" in css_content
    assert "display: none;" not in css_content[css_content.index(".badge-rec {"):css_content.index(".badge-rec {") + 100]
    assert "display: inline-flex;" in css_content[css_content.index(".badge-rec {"):css_content.index(".badge-rec {") + 100]
