import json
import re
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b24_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b24_test.db")
    database.init_db(test_db)

    # Use real config with isolated test db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_b24_card_and_modal_parity_and_review_snippet(b24_client_and_db):
    """
    P3-m05 & P3-m13:
    Both GET /api/leads/{id}/card and GET /api/leads/{id}/modal render:
    1. Exact same edit form fields.
    2. Digital channels with proper context targets.
    3. Pitch cockpit with proper IDs (pitch-cockpit-{id} vs pitch-cockpit-drawer-{id}).
    4. .review-snippet-box containing lead's review_snippet.
    """
    client, db_path = b24_client_and_db

    audit_payload = {
        "audit_completed": True,
        "flaw_codes": ["slow_load"],
        "flaw_labels": ["Slow Load Speed (3.1s)"],
        "metrics": {"load_time_ms": 3100}
    }

    snippet_text = "Exceptional customer service and lightning fast response times!"
    lead_id = database.insert_lead({
        "place_id": "ChIJ_b24_parity_01",
        "business_name": "Lone Star Elite Roofing",
        "clean_phone": "+12145557766",
        "phone": "(214) 555-7766",
        "line_type": "Mobile",
        "city": "Dallas",
        "niche": "Roofing",
        "website_url": "https://lonestarroofing.com",
        "rating": 4.9,
        "review_count": 88,
        "review_snippet": snippet_text,
        "priority_score": 86.0,
        "dm_name": "Marcus Vance",
        "dm_title": "Owner",
        "dm_email": "marcus@lonestarroofing.com",
        "generic_email": "contact@lonestarroofing.com",
        "form_url": "https://lonestarroofing.com/contact",
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    # 1. Lead card endpoint
    resp_card = client.get(f"/api/leads/{lead_id}/card")
    assert resp_card.status_code == 200
    card_html = resp_card.text

    # 2. Drawer modal endpoint
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text

    # Review snippet in both
    assert 'class="review-snippet-box"' in card_html
    assert snippet_text in card_html
    assert 'class="review-snippet-box"' in modal_html
    assert snippet_text in modal_html

    # Shared edit form fields present in both
    expected_fields = [
        'name="business_name"',
        'name="website_url"',
        'name="phone"',
        'name="line_type"',
        'name="whatsapp_url"',
        'name="city"',
        'name="niche"',
        'name="dm_name"',
        'name="dm_title"',
        'name="dm_email"',
        'name="dm_linkedin"',
        'name="generic_email"',
        'name="status"',
        'name="manual_notes"'
    ]
    for field in expected_fields:
        assert field in card_html, f"Missing {field} in lead_card.html"
        assert field in modal_html, f"Missing {field} in lead_drawer.html"

    # Specific container IDs and actions
    assert f'id="edit-panel-{lead_id}"' in card_html
    assert f'id="drawer-edit-panel-{lead_id}"' in modal_html

    assert f'id="add-channel-box-{lead_id}"' in card_html
    assert f'id="add-drawer-channel-box-{lead_id}"' in modal_html

    # Pitch cockpit IDs
    assert f'id="tracked-pitch-{lead_id}"' in card_html
    assert f'id="tracked-pitch-drawer-{lead_id}"' in modal_html

    assert f'id="pitches-data-{lead_id}"' in card_html
    assert f'id="pitches-data-drawer-{lead_id}"' in modal_html

    assert f'id="pitch-word-count-{lead_id}"' in card_html
    assert f'id="pitch-word-count-drawer-{lead_id}"' in modal_html

    assert f'id="pitch-textarea-{lead_id}"' in card_html
    assert f'id="pitch-textarea-drawer-{lead_id}"' in modal_html


def test_b24_card_displays_recent_outreach_history(b24_client_and_db):
    """
    P3-m05:
    GET /api/leads/{id}/card displays compact 'Recent Outreach History'
    showing the latest 3 touches with channel and strategy badge when lead.history exists.
    """
    client, db_path = b24_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b24_hist_01",
        "business_name": "Titan Garage Doors",
        "clean_phone": "+12145559988",
        "city": "Fort Worth",
        "priority_score": 78.0,
        "status": "contacted"
    }, db_path=db_path)

    # Log 4 touches in chronological order
    database.log_outreach(lead_id=lead_id, channel="phone", outcome="sent", notes="Initial call placed", db_path=db_path)
    database.log_outreach(lead_id=lead_id, channel="whatsapp", outcome="sent", message_sent="[naive_curiosity] Hey Titan team", db_path=db_path)
    database.log_outreach(lead_id=lead_id, channel="email", outcome="replied", message_sent="[reputation_dissonance] Stellar rating note", db_path=db_path)
    database.log_outreach(lead_id=lead_id, channel="form", outcome="sent", message_sent="[local_contrast] Quick question regarding DFW", db_path=db_path)

    # Single-lead card view
    resp = client.get(f"/api/leads/{lead_id}/card")
    assert resp.status_code == 200
    card_html = resp.text

    assert "Recent Outreach History (4)" in card_html
    assert "class=\"history-section compact-history\"" in card_html

    # Latest 3 touches should be rendered (Form, Email, WhatsApp)
    assert "FORM" in card_html
    assert "EMAIL" in card_html
    assert "WHATSAPP" in card_html
    assert "Local Contrast" in card_html
    assert "Reputation" in card_html
    assert "Curiosity" in card_html


def test_b24_style_css_pruned():
    """
    P3-m13:
    static/style.css contains .review-snippet-box and zero references to dead
    .lead-dossier-wrapper or .telemetry-grid selectors.
    """
    css_content = Path("static/style.css").read_text(encoding="utf-8")

    assert ".review-snippet-box" in css_content
    assert ".lead-dossier-wrapper" not in css_content
    assert ".telemetry-grid" not in css_content
    assert ".btn-dossier-toggle" not in css_content
    assert ".identity-info-table" not in css_content
    assert ".history-timeline-card" not in css_content


def test_b24_partial_files_exist_and_clean():
    """
    Verify the 3 extracted partial templates exist and do not contain rogue executable scripts.
    """
    partials_dir = Path("templates/partials")
    edit_form = partials_dir / "_lead_edit_form.html"
    contacts = partials_dir / "_lead_contacts.html"
    pitch_cockpit = partials_dir / "_lead_pitch_cockpit.html"

    assert edit_form.exists(), "_lead_edit_form.html must exist"
    assert contacts.exists(), "_lead_contacts.html must exist"
    assert pitch_cockpit.exists(), "_lead_pitch_cockpit.html must exist"

    # Only application/json script allowed in pitch cockpit
    for p in [edit_form, contacts]:
        content = p.read_text(encoding="utf-8")
        assert "<script" not in content, f"Unexpected script tag in {p.name}"

    cockpit_content = pitch_cockpit.read_text(encoding="utf-8")
    for script_match in re.finditer(r"<script(.*?)>", cockpit_content, re.IGNORECASE):
        assert 'type="application/json"' in script_match.group(1), f"Executable script tag in {pitch_cockpit.name}"


def test_b24_drawer_no_duplication_and_action_section_order(b24_client_and_db):
    """
    Verify lead_drawer.html renders edit form, contacts, and pitch cockpit exactly once
    (no duplicated inline markup alongside shared partials), has no duplicate select options,
    and places the quick action bar ('Update Lead Status') before 'Outreach Channel & Status Management'.
    """
    client, db_path = b24_client_and_db

    audit_payload = {
        "audit_completed": True,
        "flaw_codes": ["slow_load"],
        "flaw_labels": ["Slow Load Speed (7.1s)"],
        "metrics": {"load_time_ms": 7110}
    }
    lead_id = database.insert_lead({
        "place_id": "ChIJ_b24_dedup_01",
        "business_name": "Roof4U Roofing & Construction",
        "clean_phone": "+19704047227",
        "phone": "(970) 404-7227",
        "line_type": "Local (US)",
        "city": "Plano",
        "niche": "Roofing",
        "website_url": "https://roof4uconstruction.com",
        "rating": 5.0,
        "review_count": 204,
        "priority_score": 8.58,
        "generic_email": "office@roof4uconstruction.com",
        "status": "contacted",
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    modal_html = resp_modal.text

    # No duplicate shared sections or IDs
    assert modal_html.count('class="contacts-section"') == 1
    assert modal_html.count("<span>📞</span> Actionable Contact Info") == 1
    assert modal_html.count('class="pitch-cockpit-section"') == 1
    assert modal_html.count("<span>🎯</span> Outreach Script Generator (Touch #1)") == 1
    assert modal_html.count('class="lead-inline-edit-box"') == 1
    assert modal_html.count("Edit Business Record:") == 1
    assert modal_html.count(f'id="drawer-edit-panel-{lead_id}"') == 1
    assert modal_html.count(f'id="add-drawer-channel-box-{lead_id}"') == 1
    assert modal_html.count(f'id="pitch-cockpit-drawer-{lead_id}"') == 1
    assert modal_html.count(f'id="pitch-textarea-drawer-{lead_id}"') == 1

    # No duplicate options inside status / outcome selects in drawer
    status_select_match = re.search(
        rf'<select id="sel-status-drawer-{lead_id}".*?</select>',
        modal_html,
        re.DOTALL,
    )
    assert status_select_match is not None
    status_values = re.findall(r'<option value="([^"]+)"', status_select_match.group(0))
    assert len(status_values) == len(set(status_values))

    outcome_select_match = re.search(
        rf'<select id="sel-outcome-drawer-{lead_id}".*?</select>',
        modal_html,
        re.DOTALL,
    )
    assert outcome_select_match is not None
    outcome_values = re.findall(r'<option value="([^"]+)"', outcome_select_match.group(0))
    assert len(outcome_values) == len(set(outcome_values))

    # 'Update Lead Status' quick buttons appear before 'Outreach Channel & Status Management' in drawer
    quick_pos = modal_html.index("Update Lead Status")
    mgr_pos = modal_html.index("Outreach Channel &amp; Status Management") if "Outreach Channel &amp; Status Management" in modal_html else modal_html.index("Outreach Channel & Status Management")
    assert quick_pos < mgr_pos

    # Same section order on contacted page individual lead card
    resp_contacted = client.get("/contacted?view=grid")
    assert resp_contacted.status_code == 200
    contacted_html = resp_contacted.text
    card_quick_pos = contacted_html.index("Quick 1-Click Actions")
    card_mgr_pos = contacted_html.index("Outreach Channel & Status Management")
    assert card_quick_pos < card_mgr_pos
