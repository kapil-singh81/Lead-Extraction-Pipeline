import json
import logging
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
    test_db = str(tmp_path / "test_dashboard.db")
    database.init_db(test_db)

    # Monkeypatch load_config so dashboard uses this isolated test_db
    monkeypatch.setattr(dashboard, "load_config", lambda: {
        "app": {"db_path": test_db},
        "queue": {"daily_queue_size": 10}
    })

    return test_db


def test_p2_m01_404_guards_raise_http_exception(test_db_setup):
    """Verifies POST to missing lead on toggle-whatsapp and add-channel returns 404, not 500 NameError."""
    client = TestClient(dashboard.app, raise_server_exceptions=False)

    # 1. toggle-whatsapp with non-existent lead ID
    resp_wa = client.post("/api/leads/999999/toggle-whatsapp")
    assert resp_wa.status_code == 404
    assert resp_wa.json()["detail"] == "Lead not found"

    # 2. add-channel with non-existent lead ID
    resp_ch = client.post(
        "/api/leads/999999/add-channel",
        data={"channel_type": "facebook", "channel_url": "https://facebook.com/acme"}
    )
    assert resp_ch.status_code == 404
    assert resp_ch.json()["detail"] == "Lead not found"


def test_p2_m03_track_pitch_preserves_status_reason(test_db_setup):
    """Verifies track-pitch records copied pitch in audit_json without overwriting status_reason."""
    test_db = test_db_setup
    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_pitch_track_001",
            "business_name": "Apex Roofing",
            "status": "outreach_ready",
            "status_reason": "Flawed Site (Phone Available)",
            "clean_phone": "+12145550199",
            "audit_json": json.dumps({"flaw_codes": ["slow_load"], "score": 8.5})
        },
        db_path=test_db
    )

    client = TestClient(dashboard.app)

    # Track pitch
    resp = client.post(f"/api/leads/{lead_id}/track-pitch", data={"pitch_type": "loss_aversion"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["tracked_pitch"] == "Loss Aversion"

    # Verify lead in DB: status_reason must remain completely intact
    updated_lead = database.get_lead_by_id(lead_id, db_path=test_db)
    assert updated_lead["status_reason"] == "Flawed Site (Phone Available)"

    # Verify audit_json contains last_copied_pitch
    audit = json.loads(updated_lead["audit_json"])
    assert audit.get("last_copied_pitch") == "loss_aversion"
    assert audit.get("last_copied_pitch_label") == "Loss Aversion"
    assert audit.get("score") == 8.5

    # 404 on missing lead
    resp_404 = client.post("/api/leads/999999/track-pitch", data={"pitch_type": "loss_aversion"})
    assert resp_404.status_code == 404


def test_p2_m04_whatsapp_toggle_and_unpack_audit(test_db_setup):
    """Verifies toggling WhatsApp OFF clears both column and audit, and toggle ON with no phone does not corrupt."""
    test_db = test_db_setup

    # Case A: Lead with verified WhatsApp in both column and audit
    lead_id_a = database.insert_lead(
        {
            "place_id": "ChIJ_wa_toggle_001",
            "business_name": "Lone Star Roofs",
            "clean_phone": "+12145550199",
            "whatsapp_url": "https://wa.me/12145550199",
            "audit_json": json.dumps({
                "whatsapp_url": "https://wa.me/12145550199",
                "is_whatsapp_active": True,
                "whatsapp_source": "website_widget"
            })
        },
        db_path=test_db
    )

    client = TestClient(dashboard.app)

    # Toggle WhatsApp OFF
    resp_off = client.post(f"/api/leads/{lead_id_a}/toggle-whatsapp")
    assert resp_off.status_code == 200
    assert "WhatsApp removed as channel" in resp_off.text

    # Verify DB: both column and audit metadata are cleared / marked removed
    lead_a = database.get_lead_by_id(lead_id_a, db_path=test_db)
    assert lead_a["whatsapp_url"] is None
    audit_a = json.loads(lead_a["audit_json"])
    assert audit_a["whatsapp_url"] is None
    assert audit_a["is_whatsapp_active"] is False
    assert audit_a["whatsapp_source"] == "removed"

    # Verify unpack_audit does NOT resurrect WhatsApp
    unpacked_a = dashboard.unpack_audit(lead_a, include_pitches=False)
    assert unpacked_a["whatsapp_url"] is None

    # Case B: Lead with NO phone trying to toggle WhatsApp ON
    lead_id_b = database.insert_lead(
        {
            "place_id": "ChIJ_wa_toggle_002",
            "business_name": "No Phone Masonry",
            "clean_phone": None,
            "phone": None,
            "whatsapp_url": None,
            "audit_json": json.dumps({})
        },
        db_path=test_db
    )

    # Toggle ON with no phone
    resp_b = client.post(f"/api/leads/{lead_id_b}/toggle-whatsapp")
    assert resp_b.status_code == 200
    assert "Cannot enable WhatsApp: add a valid phone number first" in resp_b.text

    # Verify DB: whatsapp_url must NOT be set to broken "https://wa.me/"
    lead_b = database.get_lead_by_id(lead_id_b, db_path=test_db)
    assert lead_b["whatsapp_url"] is None

    # Case C: Lead with valid phone toggling ON
    lead_id_c = database.insert_lead(
        {
            "place_id": "ChIJ_wa_toggle_003",
            "business_name": "Valid Phone Roofing",
            "clean_phone": "2145550199",
            "whatsapp_url": None,
            "audit_json": json.dumps({})
        },
        db_path=test_db
    )

    resp_c = client.post(f"/api/leads/{lead_id_c}/toggle-whatsapp")
    assert resp_c.status_code == 200
    assert "WhatsApp added as channel" in resp_c.text

    lead_c = database.get_lead_by_id(lead_id_c, db_path=test_db)
    assert lead_c["whatsapp_url"] == "https://wa.me/12145550199"
    audit_c = json.loads(lead_c["audit_json"])
    assert audit_c["whatsapp_url"] == "https://wa.me/12145550199"
    assert audit_c["is_whatsapp_active"] is True
    assert audit_c["whatsapp_source"] == "manual"


def test_p2_m05_get_db_path_and_load_config_logging(caplog):
    """Verifies get_db_path returns data/lead_pipeline.db by default and load_config logs error on bad path."""
    # 1. Default fallback is data/lead_pipeline.db
    assert dashboard.get_db_path({}) == "data/lead_pipeline.db"
    assert dashboard.get_db_path({"app": {}}) == "data/lead_pipeline.db"

    # 2. Custom path respected
    assert dashboard.get_db_path({"app": {"db_path": "custom/db.sqlite"}}) == "custom/db.sqlite"

    # 3. load_config logging on unreadable/missing file
    with caplog.at_level(logging.ERROR):
        original_base = dashboard.BASE_DIR
        try:
            dashboard.BASE_DIR = dashboard.Path("/non_existent_directory_for_testing")
            cfg = dashboard.load_config()
            assert cfg == {}
            assert any("Failed to load config.json" in record.message for record in caplog.records)
        finally:
            dashboard.BASE_DIR = original_base
