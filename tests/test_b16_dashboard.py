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
    test_db = str(tmp_path / "test_b16_dashboard.db")
    database.init_db(test_db)

    # Use real config with isolated db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    return test_db


def test_p3_m01_lead_drawer_quick_actions_target_and_vals(test_db_setup):
    """
    Verifies that:
    1. GET /api/leads/{id}/modal renders Quick Action buttons targeting #lead-drawer-content
       with source="drawer" and stay="drawer" in hx-vals.
    2. #drawer-action-feedback is completely absent.
    3. Triggering a drawer action returns lead_drawer.html with flash banner, not lead_card.html.
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_b16_lead_01",
            "business_name": "Apex Modern Roofing",
            "clean_phone": "+12145550188",
            "status": "outreach_ready",
            "status_reason": "Flawed Site (Phone Available)",
            "priority_score": 8.0,
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "score": 6.5
            })
        },
        db_path=test_db
    )

    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    html = resp_modal.text

    # 1. Zero references to drawer-action-feedback
    assert "#drawer-action-feedback" not in html
    assert 'id="drawer-action-feedback"' not in html

    # 2. Buttons target #lead-drawer-content with source=drawer and stay=drawer
    assert 'hx-target="#lead-drawer-content"' in html
    assert '"source": "drawer"' in html
    assert '"stay": "drawer"' in html

    # 3. Action execution returns drawer partial
    resp_act = client.post(
        f"/api/leads/{lead_id}/action",
        data={
            "action": "contacted",
            "channel": "whatsapp",
            "source": "drawer",
            "stay": "drawer"
        }
    )
    assert resp_act.status_code == 200
    drawer_html = resp_act.text
    assert "lead-flash-banner" in drawer_html
    assert "drawer-header" in drawer_html
    # Should not contain cockpit card wrapper
    assert "cockpit-card-container" not in drawer_html


def test_p3_m02_grid_view_pitches_and_in_grid_actions(test_db_setup):
    """
    Verifies that:
    1. Grid View on /, /leads, and /contacted unpacks pitches (rendering pitch-cockpit-).
    2. Quick action buttons in Grid View target #lead-card-{id} with outerHTML and pass in_grid: true.
    3. POST /api/leads/{id}/action with in_grid='true' returns lead_card.html instead of action_response.html.
    """
    test_db = test_db_setup
    client = TestClient(dashboard.app)

    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_b16_lead_grid_01",
            "business_name": "Grid Roofing Systems",
            "city": "Plano",
            "clean_phone": "+19725550199",
            "whatsapp_url": "https://wa.me/19725550199",
            "status": "outreach_ready",
            "status_reason": "Flawed Site (WhatsApp Available)",
            "priority_score": 8.5,
            "audit_json": json.dumps({
                "audit_completed": True,
                "has_flaws": True,
                "flaw_codes": ["slow_load"],
                "score": 6.0,
                "whatsapp_url": "https://wa.me/19725550199",
                "is_whatsapp_active": True
            })
        },
        db_path=test_db
    )

    # 1. Check GET /?view=grid
    resp_q_grid = client.get("/?view=grid")
    assert resp_q_grid.status_code == 200
    assert f"pitch-cockpit-{lead_id}" in resp_q_grid.text
    assert f'hx-target="#lead-card-{lead_id}"' in resp_q_grid.text
    assert '"in_grid": "true"' in resp_q_grid.text

    # 2. Check GET /leads?view=grid
    resp_leads_grid = client.get("/leads?view=grid")
    assert resp_leads_grid.status_code == 200
    assert f"pitch-cockpit-{lead_id}" in resp_leads_grid.text
    assert f'hx-target="#lead-card-{lead_id}"' in resp_leads_grid.text

    # Contact the lead so it appears in /contacted
    database.log_outreach(lead_id=lead_id, channel="whatsapp", outcome="sent", db_path=test_db)
    database.update_lead_status(lead_id=lead_id, new_status="contacted", db_path=test_db)

    # 3. Check GET /contacted?view=grid
    resp_cont_grid = client.get("/contacted?view=grid")
    assert resp_cont_grid.status_code == 200
    assert f"pitch-cockpit-{lead_id}" in resp_cont_grid.text
    assert f'hx-target="#lead-card-{lead_id}"' in resp_cont_grid.text

    # 4. In-grid action swaps lead_card.html in place
    resp_action = client.post(
        f"/api/leads/{lead_id}/action",
        data={
            "action": "contacted",
            "channel": "phone",
            "in_grid": "true"
        }
    )
    assert resp_action.status_code == 200
    card_html = resp_action.text
    # lead_card.html has id="lead-card-{id}" and in_grid target
    assert f'id="lead-card-{lead_id}"' in card_html
    assert f'hx-target="#lead-card-{lead_id}"' in card_html
    # Should not be action_response.html (which advances the queue with next-lead counters)
    assert "lead-advance-banner" not in card_html


def test_p3_m04_daily_queue_size_zero_division_guard(tmp_path, monkeypatch):
    """
    Verifies that when daily_queue_size is configured to 0:
    1. GET / returns 200 without ZeroDivisionError.
    2. POST /api/leads/{id}/action advancing queue returns action_response.html without ZeroDivisionError.
    """
    test_db = str(tmp_path / "test_b16_zero_div.db")
    database.init_db(test_db)

    real_cfg = dashboard.load_config()
    cfg_zero = json.loads(json.dumps(real_cfg))
    cfg_zero["app"]["db_path"] = test_db
    cfg_zero["queue"]["daily_queue_size"] = 0
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_zero)

    lead_id = database.insert_lead(
        {
            "place_id": "ChIJ_b16_zero_div_01",
            "business_name": "Zero Div Roofing",
            "status": "outreach_ready",
            "clean_phone": "+12145550177"
        },
        db_path=test_db
    )

    client = TestClient(dashboard.app)

    # 1. Base template progress widget render with daily_queue_size == 0
    resp_home = client.get("/")
    assert resp_home.status_code == 200
    assert "Reviewed Today" in resp_home.text

    # 2. Action response progress widget render with daily_queue_size == 0
    resp_action = client.post(
        f"/api/leads/{lead_id}/action",
        data={"action": "contacted", "channel": "phone"}
    )
    assert resp_action.status_code == 200
    assert "Reviewed Today" in resp_action.text
