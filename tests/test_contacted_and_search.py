import json
import os
import sys
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import dashboard
from core import database
from core.pitch_generator import generate_pitches


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Creates an isolated SQLite database seeded with lead #51 and a contacted lead."""
    db_path = str(tmp_path / "test_contacted_search.db")
    database.init_db(db_path)

    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = db_path
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    conn = database.get_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO leads (
                    id, place_id, business_name, niche, city, country,
                    website_url, clean_domain, phone, clean_phone, line_type,
                    facebook_url, rating, review_count, status, status_reason,
                    priority_score, audit_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    51,
                    "ChIJ_test_lead_51",
                    "Storm Shield Roofing",
                    "Roofing",
                    "Dallas",
                    "US",
                    "https://stormshieldroofing.example.com",
                    "stormshieldroofing.example.com",
                    "(817) 764-5775",
                    "+18177645775",
                    "Mobile",
                    "https://facebook.com/stormshieldroofing",
                    4.9,
                    48,
                    "outreach_ready",
                    "Flawed Site (Phone Available)",
                    82.5,
                    json.dumps({
                        "audit_completed": True,
                        "has_website": True,
                        "flaw_codes": ["broken_mobile"],
                        "flaw_labels": ["Broken Mobile Layout (Horizontal Overflow)"],
                    }),
                ),
            )
    finally:
        conn.close()

    # Seed a contacted lead so contacted page and stats always have realistic history
    contacted_id = database.insert_lead(
        {
            "place_id": "ChIJ_test_contacted_52",
            "business_name": "Metro Crest Roofers",
            "niche": "Roofing",
            "city": "Fort Worth",
            "country": "US",
            "clean_phone": "+12145550188",
            "line_type": "Mobile",
            "rating": 4.8,
            "review_count": 30,
            "status": "outreach_ready",
            "priority_score": 70.0,
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_website": True,
                "flaw_codes": ["slow_load"],
                "flaw_labels": ["Slow Load (7.2s)"],
            }),
        },
        db_path=db_path,
    )
    database.log_outreach(
        lead_id=contacted_id,
        channel="whatsapp",
        outcome="sent",
        message_sent="[naive_curiosity] Quick question",
        db_path=db_path,
    )

    return db_path


def test_omni_search_and_filters(isolated_db):
    """Verifies that database.get_leads_master searches across all lead fields."""
    db_path = isolated_db

    # 1. Search by name
    leads, total = database.get_leads_master(search="Storm", db_path=db_path)
    assert total > 0
    assert any("storm" in l["business_name"].lower() for l in leads)

    # 2. Search by city
    leads, total = database.get_leads_master(search="Dallas", db_path=db_path)
    assert total > 0

    # 3. Search by ID (e.g. #51)
    leads, total = database.get_leads_master(search="51", db_path=db_path)
    assert total > 0
    assert any(l["id"] == 51 for l in leads)

    # 4. Filter by channel (WhatsApp) - strictly verified leads only
    database.update_lead_fields(51, {"whatsapp_url": "https://wa.me/18177645775"}, db_path=db_path)
    leads, total = database.get_leads_master(channel="whatsapp", db_path=db_path)
    assert total > 0
    unpacked = [dashboard.unpack_audit(l, db_path=db_path) for l in leads]
    assert all(bool(l["whatsapp_url"]) for l in unpacked)
    database.update_lead_fields(51, {"whatsapp_url": None}, db_path=db_path)

    # 5. Filter by min_rating (4.8)
    leads, total = database.get_leads_master(min_rating=4.8, db_path=db_path)
    assert total > 0
    assert all(l["rating"] is not None and l["rating"] >= 4.8 for l in leads)


def test_contacted_leads_and_stats(isolated_db):
    """Verifies retrieval of contacted leads with history and KPI stats."""
    db_path = isolated_db

    stats = database.get_contacted_stats(db_path=db_path)
    assert "total" in stats
    assert "whatsapp" in stats
    assert "phone" in stats
    assert "email" in stats
    assert "form" in stats
    assert "facebook" in stats
    assert stats["total"] >= 1
    assert stats["whatsapp"] >= 1
    assert stats["facebook"] >= 0
    breakdown_sum = (
        stats["whatsapp"] + stats["phone"] + stats["email"] +
        stats["form"] + stats["facebook"] + stats.get("other", 0)
    )
    assert stats["total"] == breakdown_sum

    contacted_leads, total = database.get_contacted_leads_with_history(db_path=db_path)
    assert total >= 1
    lead = contacted_leads[0]
    assert "last_channel" in lead
    assert "contacted_at" in lead


def test_no_oob_in_lead_card(isolated_db):
    """Verifies that GET /api/leads/{id}/card returns ONLY the card without out-of-band swaps."""
    client = TestClient(dashboard.app)

    resp = client.get("/api/leads/51/card", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    html = resp.text

    # Must NOT contain out-of-band swap wrappers inside lead card
    assert 'id="sidebar-counts" hx-swap-oob="true"' not in html
    assert 'id="queue-tiles-container" hx-swap-oob="true"' not in html
    assert 'id="progress-tracker" hx-swap-oob="true"' not in html


def test_action_response_has_oob(isolated_db):
    """Verifies that POST /api/leads/{id}/action returns action_response with proper OOB swaps."""
    client = TestClient(dashboard.app)

    resp = client.post(
        "/api/leads/51/action",
        data={"action": "contacted", "channel": "whatsapp"},
        headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    html = resp.text

    # Must contain OOB swap wrappers for sidebar and queue
    assert 'id="sidebar-counts" hx-swap-oob="true"' in html
    assert 'id="queue-tiles-container" hx-swap-oob="true"' in html
    assert 'id="progress-tracker" hx-swap-oob="true"' in html


def test_contacted_page_endpoint(isolated_db):
    """Verifies that the /contacted route renders successfully with KPIs, view toggles, and individual cards."""
    client = TestClient(dashboard.app)

    # 1. Table view
    resp = client.get("/contacted?view=table")
    assert resp.status_code == 200
    assert "Contacted Leads Log" in resp.text
    assert "Total Contacted" in resp.text
    assert "WhatsApp Touches" in resp.text
    assert "Facebook Sent" in resp.text
    assert "crm-table" in resp.text
    assert "Cards Grid" in resp.text

    # 2. Cards Grid view (Individual Cards on Contacted Page)
    resp_grid = client.get("/contacted?view=grid")
    assert resp_grid.status_code == 200
    assert "all-cards-grid" in resp_grid.text
    assert "Table View" in resp_grid.text
    assert "lead-card" in resp_grid.text or "No Contacted Leads Found" in resp_grid.text


def test_multi_flaw_and_spacious_message():
    """Verifies multi-flaw synthesis (top 2-3 issues) and paragraph spacing (\n\n)."""
    lead_with_6_flaws = {
        "business_name": "Lone Star Commercial Roofing LLC",
        "city": "Dallas",
        "rating": 4.9,
        "review_count": 88,
        "clean_phone": "+12145550199",
        "website_url": "https://lonestarroofing.example.com",
        "audit": {
            "flaw_codes": [
                "thin_architecture",
                "content_starved",
                "broken_mobile",
                "unclickable_phone",
                "missing_lead_capture",
                "zero_visual_proof"
            ]
        }
    }

    pitches = generate_pitches(lead_with_6_flaws)
    flaw = pitches["specific_flaw"]

    # Top conversion flaw must take precedence over thin architecture and be single highest priority
    assert "barely explains" not in flaw
    assert "the layout cuts off sideways on my phone" in flaw

    # Check spacious formatting: all scripts must have paragraph breaks (\n\n)
    for key, app_dict in pitches["approaches"].items():
        text = app_dict["text"]
        assert "\n\n" in text, f"Script {key} must contain paragraph breaks"
        paragraphs = text.split("\n\n")
        assert len(paragraphs) >= 2, f"Script {key} must have at least 2 distinct paragraphs"
        assert app_dict["word_count"] <= 70
        assert "http" not in text


def test_database_update_lead_fields(isolated_db):
    """Verifies arbitrary field correction and clean domain/phone formatting in database."""
    db_path = isolated_db

    updated = database.update_lead_fields(
        lead_id=51,
        fields={
            "dm_name": "Marcus Vance",
            "dm_title": "Commercial Operations VP",
            "dm_email": "marcus@csirenovations.com",
            "manual_notes": "Verified decision maker via phone inspection"
        },
        db_path=db_path
    )
    assert updated is not None
    assert updated["dm_name"] == "Marcus Vance"
    assert updated["dm_title"] == "Commercial Operations VP"
    assert updated["dm_email"] == "marcus@csirenovations.com"
    assert updated["manual_notes"] == "Verified decision maker via phone inspection"


def test_lead_update_api(isolated_db):
    """Verifies that POST /api/leads/{id}/update saves data and returns updated card."""
    client = TestClient(dashboard.app)

    resp = client.post(
        "/api/leads/51/update",
        data={
            "business_name": "CSI Commercial Renovations & Roofing",
            "dm_name": "Marcus Vance",
            "manual_notes": "Corrected contractor title"
        }
    )
    import html
    assert resp.status_code == 200
    unescaped = html.unescape(resp.text)
    assert "CSI Commercial Renovations & Roofing" in unescaped
    assert "Marcus Vance" in unescaped
    assert "Lead details updated and saved to SQLite!" in unescaped


def test_status_action_stay_mode(isolated_db):
    """Verifies that submitting an outreach action with stay='true' updates status without moving away."""
    client = TestClient(dashboard.app)

    client.post(
        "/api/leads/51/update",
        data={"business_name": "CSI Commercial Renovations & Roofing"},
    )
    resp = client.post(
        "/api/leads/51/action",
        data={
            "action": "contacted",
            "channel": "whatsapp",
            "outcome": "replied",
            "notes": "Discussed broken mobile layout with owner",
            "stay": "true"
        }
    )
    import html
    assert resp.status_code == 200
    assert 'id="lead-card-51"' in resp.text
    unescaped = html.unescape(resp.text)
    assert "Status updated to 'Contacted'" in unescaped
    assert "CSI Commercial Renovations & Roofing" in unescaped


def test_lead_card_contains_edit_and_clean_dossier(isolated_db):
    """Verifies that the main lead card has edit trigger and add-channel button, while dossier telemetry and edit notice are removed."""
    client = TestClient(dashboard.app)

    resp = client.get("/api/leads/51/card")
    assert resp.status_code == 200
    html = resp.text

    assert "btn-edit-trigger" in html
    assert 'id="edit-panel-51"' in html
    assert "Edit Mode Active:" not in html
    assert 'action="/api/leads/51/update"' in html or 'hx-post="/api/leads/51/update"' in html

    assert "btn-add-channel-plus" in html
    assert "add-channel-box-51" in html

    assert 'id="sel-channel-51"' not in html
    assert "Complete Lead Details & Technical Telemetry" not in html
    assert 'id="dossier-body-51"' not in html
    assert "Deep Technical Audit Telemetry" not in html

    database.log_outreach(
        lead_id=51,
        channel="whatsapp",
        outcome="sent",
        message_sent="[naive_curiosity] Initial check",
        db_path=isolated_db,
    )
    resp_drawer = client.get("/api/leads/51/modal")
    assert resp_drawer.status_code == 200
    assert "Outreach Channel & Status Management" in resp_drawer.text
    assert 'id="sel-channel-drawer-51"' in resp_drawer.text
    assert "Save Status & Log Touch" in resp_drawer.text
    assert "Edit Mode Active:" not in resp_drawer.text
    assert "btn-delete-log" in resp_drawer.text


def test_whatsapp_channel_toggle(isolated_db):
    """Verifies that WhatsApp channel can be dynamically added/removed via Digital Channels toggle and 1-click WhatsApp buttons persist."""
    client = TestClient(dashboard.app)
    db_path = isolated_db

    database.update_lead_fields(51, {"whatsapp_url": None}, db_path=db_path)

    resp = client.get("/api/leads/51/card")
    assert resp.status_code == 200
    assert "+ WhatsApp" in resp.text
    assert "Open WhatsApp ↗" in resp.text
    assert "WB Sent" in resp.text

    resp_on = client.post("/api/leads/51/toggle-whatsapp")
    assert resp_on.status_code == 200
    assert "Remove WhatsApp as an active channel" in resp_on.text
    assert "Open WhatsApp ↗" in resp_on.text
    assert "WB Sent" in resp_on.text
    assert "wa.me" in resp_on.text

    l = database.get_lead_by_id(51, db_path=db_path)
    assert l["whatsapp_url"] is not None and "wa.me" in l["whatsapp_url"]

    resp_off = client.post("/api/leads/51/toggle-whatsapp")
    assert resp_off.status_code == 200
    assert "+ WhatsApp" in resp_off.text
    assert "Open WhatsApp ↗" in resp_off.text

    l_off = database.get_lead_by_id(51, db_path=db_path)
    assert l_off["whatsapp_url"] is None


def test_facebook_action_and_channel_filter(isolated_db):
    """Verifies marking lead contacted via Facebook, logging outreach, and filtering by Facebook."""
    client = TestClient(dashboard.app)
    db_path = isolated_db
    test_lead_id = 51

    resp_card = client.get(f"/api/leads/{test_lead_id}/card")
    assert resp_card.status_code == 200
    assert "FB Sent" in resp_card.text

    resp = client.post(
        f"/api/leads/{test_lead_id}/mark-facebook",
        data={
            "pitch_type": "naive_curiosity",
            "message_text": "Is it by design that your contact button cuts off on mobile?",
            "notes": "Testing Facebook outreach",
            "stay": "true"
        },
        headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200

    updated_lead = database.get_lead_by_id(test_lead_id, db_path=db_path)
    assert updated_lead["status"] == "contacted"
    assert "facebook" in updated_lead["status_reason"].lower()

    history = database.get_lead_outreach_history(test_lead_id, db_path=db_path)
    assert any(h["channel"].lower() == "facebook" for h in history)

    resp_fb = client.get("/contacted?channel=facebook")
    assert resp_fb.status_code == 200
    assert "Facebook" in resp_fb.text


def test_contacted_table_multi_channel_icons_and_reply_analytics(tmp_path, monkeypatch):
    """
    Verifies:
    1. Contacted table shows icon-only badges for all contacted platforms + single latest timestamp + distinct Direct Contact Info.
    2. Changing outcome from sent to replied on one channel credits +1 only to that channel and its strategy in reply analytics.
    """
    import re

    test_db = str(tmp_path / "test_multi_ch_replies.db")
    database.init_db(test_db)

    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    lead_id = database.insert_lead({
        "place_id": "ChIJ_multi_reply_01",
        "business_name": "Multi Channel Roofing Co",
        "clean_phone": "+12145553344",
        "dm_email": "owner@multichannelroofing.com",
        "facebook_url": "https://facebook.com/multichannelroofing",
        "form_url": "https://multichannelroofing.com/contact",
        "status": "outreach_ready",
        "priority_score": 85.0,
    }, db_path=test_db)

    id_form = database.log_outreach(lead_id, "form", outcome="sent", message_sent="[local_contrast] Form pitch", db_path=test_db)
    id_email = database.log_outreach(lead_id, "email", outcome="sent", message_sent="[naive_curiosity] Email pitch", db_path=test_db)
    id_fb = database.log_outreach(lead_id, "facebook", outcome="sent", message_sent="[accusation_audit] FB pitch", db_path=test_db)

    conn = database.get_connection(test_db)
    with conn:
        conn.execute("UPDATE outreach_log SET sent_at = '2026-11-24 10:00:00' WHERE id = ?", (id_form,))
        conn.execute("UPDATE outreach_log SET sent_at = '2026-11-24 11:00:00' WHERE id = ?", (id_email,))
        conn.execute("UPDATE outreach_log SET sent_at = '2026-11-24 12:00:00' WHERE id = ?", (id_fb,))
    conn.close()

    client = TestClient(dashboard.app)

    resp_tbl = client.get("/contacted?view=table")
    assert resp_tbl.status_code == 200
    row_match = re.search(rf'<tr[^>]*id="row-contacted-{lead_id}".*?</tr>', resp_tbl.text, re.DOTALL)
    assert row_match is not None
    row_html = row_match.group(0)

    assert "tbl-ch-icon ch-form" in row_html
    assert "tbl-ch-icon ch-email" in row_html
    assert "tbl-ch-icon ch-facebook" in row_html
    assert row_html.count("⏱️") == 1
    assert "⏱️ 2026-11-24 12:00:00" in row_html
    assert "owner@multichannelroofing.com" in row_html
    assert "+12145553344" in row_html

    resp_reply = client.post(
        f"/api/leads/{lead_id}/action",
        data={
            "action": "contacted",
            "channel": "email",
            "outcome": "replied",
            "pitch_type": "local_contrast",
            "notes": "Prospect replied via email",
            "source": "drawer",
            "stay": "drawer",
        },
    )
    assert resp_reply.status_code == 200

    resp_analytics = client.get("/api/analytics/replies")
    assert resp_analytics.status_code == 200
    analytics = resp_analytics.json()

    assert analytics["total_replied_leads"] == 1
    assert analytics["by_channel"]["email"]["replied_leads"] == 1
    assert analytics["by_channel"]["form"]["replied_leads"] == 0
    assert analytics["by_channel"]["facebook"]["replied_leads"] == 0

    assert analytics["by_strategy"]["naive_curiosity"]["replied_leads"] == 1
    assert analytics["by_strategy"]["local_contrast"]["replied_leads"] == 0
    assert analytics["by_strategy"]["accusation_audit"]["replied_leads"] == 0

    client.post(
        f"/api/leads/{lead_id}/action",
        data={
            "action": "contacted",
            "channel": "email",
            "outcome": "replied",
            "notes": "Follow-up note",
            "source": "drawer",
            "stay": "drawer",
        },
    )
    analytics_after = client.get("/api/analytics/replies").json()
    assert analytics_after["total_replied_leads"] == 1
    assert analytics_after["by_channel"]["email"]["replied_leads"] == 1
    assert analytics_after["by_strategy"]["naive_curiosity"]["replied_leads"] == 1
