import json
import os
import sys
import pytest
from starlette.testclient import TestClient

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import dashboard
from core import database


@pytest.fixture
def test_db_setup(tmp_path, monkeypatch):
    """Sets up an isolated test database in tmp_path and monkeypatches dashboard.load_config."""
    test_db = str(tmp_path / "test_b15_dashboard.db")
    database.init_db(test_db)

    # Use real config with isolated db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    return test_db


def test_p2_m07_contact_addition_reclassifies_and_deduplicates_phone(test_db_setup):
    """
    Verifies that:
    1. Adding dm_email via POST /api/leads/{id}/update reclassifies a missing_info lead to outreach_ready.
    2. Passing phone and clean_phone simultaneously derives clean_phone without duplication, and clearing phone clears clean_phone.
    3. Adding facebook via POST /api/leads/{id}/add-channel reclassifies a missing_info lead to outreach_ready.
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    # 1. Seed flawed lead with NO contact info -> missing_info
    lead_id_1 = database.insert_lead(
        {
            "place_id": "ChIJ_b15_lead_01",
            "business_name": "Lone Star Plumbing",
            "website_url": "https://lonestarplumbing.com",
            "status": "missing_info",
            "status_reason": "Missing Contact Information",
            "priority_score": 0.0,
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "load_time_s": 5.2,
                "score": 6.5
            })
        },
        db_path=test_db
    )

    # Update with dm_email
    resp_update = client.post(
        f"/api/leads/{lead_id_1}/update",
        data={"dm_email": "owner@lonestarplumbing.com"}
    )
    assert resp_update.status_code == 200

    lead_1 = database.get_lead_by_id(lead_id_1, db_path=test_db)
    assert lead_1["status"] == "outreach_ready", f"Expected outreach_ready, got {lead_1['status']}"
    assert lead_1["priority_score"] > 0, "Expected priority_score to be re-calculated > 0"
    assert lead_1["dm_email"] == "owner@lonestarplumbing.com"

    # 2. Test phone deduplication and derivation
    resp_phone = client.post(
        f"/api/leads/{lead_id_1}/update",
        data={"phone": "(214) 555-0199", "clean_phone": "IGNORED_UNSANITIZED"}
    )
    assert resp_phone.status_code == 200
    lead_phone = database.get_lead_by_id(lead_id_1, db_path=test_db)
    assert lead_phone["phone"] == "(214) 555-0199"
    assert lead_phone["clean_phone"] == "+12145550199"  # Database cleanly derived it

    # Clearing phone clears both phone and clean_phone
    resp_clear = client.post(
        f"/api/leads/{lead_id_1}/update",
        data={"phone": ""}
    )
    assert resp_clear.status_code == 200
    lead_cleared = database.get_lead_by_id(lead_id_1, db_path=test_db)
    assert lead_cleared["phone"] is None
    assert lead_cleared["clean_phone"] is None

    # 3. Test add-channel facebook reclassifies missing_info lead
    lead_id_2 = database.insert_lead(
        {
            "place_id": "ChIJ_b15_lead_02",
            "business_name": "Lone Star HVAC",
            "website_url": "https://lonestarhvac.com",
            "status": "missing_info",
            "status_reason": "Missing Contact Information",
            "priority_score": 0.0,
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "load_time_s": 4.8,
                "score": 6.0
            })
        },
        db_path=test_db
    )

    resp_fb = client.post(
        f"/api/leads/{lead_id_2}/add-channel",
        data={"channel_type": "facebook", "channel_url": "https://facebook.com/lonestarhvac"}
    )
    assert resp_fb.status_code == 200
    lead_2 = database.get_lead_by_id(lead_id_2, db_path=test_db)
    assert lead_2["status"] == "outreach_ready", f"Expected outreach_ready, got {lead_2['status']}"
    assert lead_2["priority_score"] > 0, "Expected priority_score > 0 after adding facebook"
    assert lead_2["facebook_url"] == "https://facebook.com/lonestarhvac"


def test_p2_m08_action_contacted_logs_recommended_strategy_when_pitch_type_omitted(test_db_setup):
    """
    Verifies that POST /api/leads/{id}/action with action='contacted' and omitted pitch_type
    logs the lead's true recommended strategy key ([local_contrast] for a 5.0★ / 80-review lead),
    not the hardcoded fallback [naive_curiosity].
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    # 5.0 rating and 80 reviews triggers local_contrast
    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_b15_lead_pitch_01",
            "business_name": "Dallas Elite Roofing",
            "rating": 5.0,
            "review_count": 80,
            "status": "outreach_ready",
            "status_reason": "Flawed Site (Phone Available)",
            "phone": "(214) 555-0155",
            "clean_phone": "+12145550155",
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "load_time_s": 5.8
            })
        },
        db_path=test_db
    )

    resp = client.post(
        f"/api/leads/{lead_id}/action",
        data={"action": "contacted", "channel": "phone"}
    )
    assert resp.status_code == 200

    history = database.get_lead_outreach_history(lead_id, db_path=test_db)
    assert len(history) == 1, "Expected 1 outreach history log entry"
    assert "[local_contrast]" in history[0]["message_sent"], (
        f"Expected [local_contrast] in logged message, got: {history[0]['message_sent']}"
    )
    assert "[naive_curiosity]" not in history[0]["message_sent"]


def test_p2_m09_search_selects_first_filtered_lead_and_empty_on_zero_match(test_db_setup):
    """
    Verifies that searching on /:
    1. Selects the first matching lead from the filtered queue when lead_id is None.
    2. Renders empty queue state when search matches zero leads.
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    # Seed two leads in different cities
    lead_austin = database.insert_lead(
        {
            "place_id": "ChIJ_b15_austin_01",
            "business_name": "Austin Apex Roofing",
            "city": "Austin",
            "status": "outreach_ready",
            "priority_score": 9.0,
            "clean_phone": "+15125550101",
            "audit_json": json.dumps({"audit_completed": True, "has_flaws": True, "flaw_codes": ["slow_load"]})
        },
        db_path=test_db
    )
    lead_plano = database.insert_lead(
        {
            "place_id": "ChIJ_b15_plano_01",
            "business_name": "Plano Premier Roofing",
            "city": "Plano",
            "status": "outreach_ready",
            "priority_score": 8.0,
            "clean_phone": "+19725550102",
            "audit_json": json.dumps({"audit_completed": True, "has_flaws": True, "flaw_codes": ["slow_load"]})
        },
        db_path=test_db
    )

    # 1. Search for Plano: Plano Premier Roofing must be the selected lead in Cockpit card
    resp_plano = client.get("/?search=Plano")
    assert resp_plano.status_code == 200
    # Plano Premier Roofing should be in the card header and verification details
    assert "Plano Premier Roofing" in resp_plano.text
    # Austin Apex Roofing was not in the filtered queue
    assert "Austin Apex Roofing" not in resp_plano.text

    # 2. Search for non-existent: empty queue state
    resp_empty = client.get("/?search=NonexistentXYZ")
    assert resp_empty.status_code == 200
    assert "Queue Complete" in resp_empty.text or "No leads match" in resp_empty.text or "Daily Queue Complete" in resp_empty.text


def test_p2_m02_delete_outreach_reverts_status_or_updates_reason(test_db_setup):
    """
    Verifies that deleting an outreach log entry via POST /api/leads/{id}/outreach/{log_id}/delete:
    1. Reverts lead from contacted back to outreach_ready (or classifier status) when deleting sole entry.
    2. Keeps lead contacted and updates status_reason to remaining entry's channel when deleting 1 of 2 entries.
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    # Case A: Sole outreach entry deletion
    lead_a = database.insert_lead(
        {
            "place_id": "ChIJ_b15_delete_01",
            "business_name": "Solitary Roof Co",
            "status": "contacted",
            "status_reason": "Contacted via phone",
            "clean_phone": "+12145550111",
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "score": 7.0
            })
        },
        db_path=test_db
    )
    database.log_outreach(lead_id=lead_a, channel="phone", outcome="sent", db_path=test_db)
    history_a = database.get_lead_outreach_history(lead_a, db_path=test_db)
    assert len(history_a) == 1
    log_id_a = history_a[0]["id"]

    resp_del_a = client.post(f"/api/leads/{lead_a}/outreach/{log_id_a}/delete")
    assert resp_del_a.status_code == 200
    lead_a_reverted = database.get_lead_by_id(lead_a, db_path=test_db)
    assert lead_a_reverted["status"] == "outreach_ready", (
        f"Expected outreach_ready after deleting sole log, got {lead_a_reverted['status']}"
    )

    # Case B: Multi-entry outreach deletion (deleting 1 of 2)
    lead_b = database.insert_lead(
        {
            "place_id": "ChIJ_b15_delete_02",
            "business_name": "Dual Channel Roof Co",
            "status": "contacted",
            "status_reason": "Contacted via whatsapp",
            "clean_phone": "+12145550122",
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "score": 7.0
            })
        },
        db_path=test_db
    )
    database.log_outreach(lead_id=lead_b, channel="email", outcome="sent", message_sent="Email touch", db_path=test_db)
    database.log_outreach(lead_id=lead_b, channel="whatsapp", outcome="sent", message_sent="WA touch", db_path=test_db)

    history_b = database.get_lead_outreach_history(lead_b, db_path=test_db)
    assert len(history_b) == 2
    # history_b[0] is the latest (whatsapp), history_b[1] is the earlier (email)
    latest_log_id = history_b[0]["id"]

    resp_del_b = client.post(f"/api/leads/{lead_b}/outreach/{latest_log_id}/delete")
    assert resp_del_b.status_code == 200
    lead_b_after = database.get_lead_by_id(lead_b, db_path=test_db)
    assert lead_b_after["status"] == "contacted", "Lead should remain contacted"
    assert "email" in lead_b_after["status_reason"].lower(), (
        f"Expected remaining channel 'email' in status_reason, got {lead_b_after['status_reason']}"
    )
