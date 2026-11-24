import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b18_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b18_test.db")
    database.init_db(test_db)

    # Use real config with isolated db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m05_and_p3_m10_queue_tiles_and_queue_html(b18_client_and_db):
    """
    P3-M05 & P3-m10:
    1. queue_tiles.html renders data-phone="true"/"false" and setActiveTile(this) without static indices.
    2. queue.html ignores SELECT in keydown, checks data-phone === 'true' for the phone pill,
       and hooks htmx:afterSwap to re-filter and updateCounterDisplay.
    """
    client, db_path = b18_client_and_db

    # Lead 1: has clean_phone
    database.insert_lead({
        "place_id": "ChIJ_b18_lead_phone",
        "business_name": "Lone Star Metal Roofing",
        "clean_phone": "+12145550188",
        "phone": "(214) 555-0188",
        "status": "outreach_ready",
        "priority_score": 85.0,
        "audit_json": json.dumps({"audit_completed": True, "flaw_codes": ["slow_site"]})
    }, db_path=db_path)

    # Lead 2: no phone
    database.insert_lead({
        "place_id": "ChIJ_b18_lead_nophone",
        "business_name": "Apex Digital Studio",
        "clean_phone": None,
        "phone": None,
        "status": "outreach_ready",
        "priority_score": 80.0,
        "audit_json": json.dumps({"audit_completed": True, "flaw_codes": []})
    }, db_path=db_path)

    resp = client.get("/?view=split")
    assert resp.status_code == 200
    html = resp.text

    # 1. Verification of queue_tiles.html rendering
    assert 'data-phone="true"' in html
    assert 'data-phone="false"' in html
    assert 'onclick="setActiveTile(this)"' in html
    assert 'onclick="setActiveTile(this,' not in html

    # 2. Verification of queue.html client JS
    assert "['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)" in html
    assert "tile.getAttribute('data-phone') === 'true'" in html
    assert "updateCounterDisplay()" in html
    assert "htmx:afterSwap" in html
    assert "getVisibleTiles()" in html


def test_p3_m09_action_response_oob_queue_counts(b18_client_and_db):
    """
    P3-M09: action_response.html outputs hx-swap-oob="true" updates for
    #queue-toolbar-count and #queue-nav-count whenever queue_leads is defined.
    """
    client, db_path = b18_client_and_db

    lead1_id = database.insert_lead({
        "place_id": "ChIJ_b18_lead_act_1",
        "business_name": "Apex Commercial Roofing",
        "clean_phone": "+12145550111",
        "status": "outreach_ready",
        "priority_score": 90.0,
        "audit_json": json.dumps({"audit_completed": True, "flaw_codes": ["broken_mobile"]})
    }, db_path=db_path)

    lead2_id = database.insert_lead({
        "place_id": "ChIJ_b18_lead_act_2",
        "business_name": "Dallas Elite Solar",
        "clean_phone": "+12145550222",
        "status": "outreach_ready",
        "priority_score": 70.0,
        "audit_json": json.dumps({"audit_completed": True, "flaw_codes": []})
    }, db_path=db_path)

    # Post contacted action for lead 1 from split queue view
    resp = client.post(
        f"/api/leads/{lead1_id}/action",
        data={"action": "contacted", "channel": "whatsapp", "stay": "false"}
    )
    assert resp.status_code == 200
    html = resp.text

    # Expect OOB count badges with 1 remaining lead in queue
    assert '<strong id="queue-toolbar-count" hx-swap-oob="true">1</strong>' in html
    assert '<span id="queue-nav-count" class="badge-mini" hx-swap-oob="true">1</span>' in html
    assert '<div id="queue-tiles-container" hx-swap-oob="true"' in html


def test_p3_m09_post_lead_update_card_view_returns_oob_queue_tiles(b18_client_and_db):
    """
    P3-M09: POST /api/leads/{id}/update (from card view) returns an OOB #queue-tiles-container
    block reflecting the updated business name and chips on the left navigator tile.
    """
    client, db_path = b18_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b18_lead_edit",
        "business_name": "Original Name Roofing",
        "clean_phone": "+12145550333",
        "status": "outreach_ready",
        "priority_score": 88.0,
        "audit_json": json.dumps({"audit_completed": True, "flaw_codes": []})
    }, db_path=db_path)

    # Post lead update from card view (no source="drawer")
    resp = client.post(
        f"/api/leads/{lead_id}/update",
        data={
            "business_name": "Renamed Pinnacle Roofing",
            "phone": "(214) 555-7777",
            "city": "Fort Worth"
        }
    )
    assert resp.status_code == 200
    html = resp.text

    # Verify OOB queue tiles container is present
    assert '<div id="queue-tiles-container" hx-swap-oob="true" class="queue-tiles-scroll-box">' in html
    # Verify the updated business name appears inside the returned HTML
    assert "Renamed Pinnacle Roofing" in html
