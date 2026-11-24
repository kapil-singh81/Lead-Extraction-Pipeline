import json
import os
import sys
import time
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
    test_db = str(tmp_path / "test_b14_dashboard.db")
    database.init_db(test_db)

    # Monkeypatch load_config so dashboard uses this isolated test_db
    monkeypatch.setattr(dashboard, "load_config", lambda: {
        "app": {"db_path": test_db},
        "queue": {"daily_queue_size": 10}
    })

    return test_db


def test_p2_m06_unpack_audit_eliminates_n_plus_one_history_queries(test_db_setup, monkeypatch):
    """
    Verifies that unpack_audit does not issue N+1 SQLite queries for lead outreach history
    when rendering bulk lists on /leads, /contacted, and queue card lists.
    """
    test_db = test_db_setup

    # Seed 5 leads
    lead_ids = []
    for i in range(5):
        lid = database.insert_lead(
            {
                "place_id": f"ChIJ_b14_lead_{i}",
                "business_name": f"Roofing Co {i}",
                "status": "outreach_ready" if i < 3 else "contacted",
                "status_reason": "Verified Contact",
                "clean_phone": f"+1214555010{i}",
                "audit_json": json.dumps({"flaw_codes": ["slow_load"], "score": 7.0 + i})
            },
            db_path=test_db
        )
        lead_ids.append(lid)
        if i >= 3:
            database.log_outreach(
                lead_id=lid,
                channel="whatsapp",
                outcome="sent",
                message_sent=f"Message {i}",
                db_path=test_db
            )

    client = TestClient(dashboard.app)

    # Spy on database.get_lead_outreach_history
    history_calls = []
    original_get_history = database.get_lead_outreach_history

    def spied_get_history(lead_id, limit=50, db_path=None):
        history_calls.append((lead_id, db_path))
        return original_get_history(lead_id, limit=limit, db_path=db_path)

    monkeypatch.setattr(database, "get_lead_outreach_history", spied_get_history)

    # 1. GET /leads (Master leads archive: 5 leads rendered)
    history_calls.clear()
    resp_leads = client.get("/leads")
    assert resp_leads.status_code == 200
    assert len(history_calls) == 0, f"Expected 0 history queries for /leads, got {len(history_calls)}"

    # 2. GET /contacted (Contacted leads archive: 2 contacted leads rendered)
    history_calls.clear()
    resp_contacted = client.get("/contacted")
    assert resp_contacted.status_code == 200
    assert len(history_calls) == 0, f"Expected 0 history queries for /contacted, got {len(history_calls)}"

    # 3. GET / (Daily queue view: queue_leads loop must have include_history=False; only selected_lead queries history)
    history_calls.clear()
    resp_queue = client.get("/")
    assert resp_queue.status_code == 200
    # At most 1 call for the single selected lead
    assert len(history_calls) <= 1, f"Expected at most 1 history query for selected lead on /, got {len(history_calls)}"


def test_p2_m01_lead_card_and_modal_404_on_missing_lead(test_db_setup):
    """Verifies GET /api/leads/{id}/card and /api/leads/{id}/modal return 404 for non-existent leads."""
    client = TestClient(dashboard.app, raise_server_exceptions=False)

    # Missing lead on card endpoint
    resp_card = client.get("/api/leads/999999/card")
    assert resp_card.status_code == 404
    assert resp_card.json()["detail"] == "Lead not found"

    # Missing lead on modal/drawer endpoint
    resp_modal = client.get("/api/leads/999999/modal")
    assert resp_modal.status_code == 404
    assert resp_modal.json()["detail"] == "Lead not found"


def test_p2_m04_lifespan_and_unused_import_removal(tmp_path, monkeypatch):
    """
    Verifies that:
    1. dashboard.py uses FastAPI lifespan context manager instead of deprecated @app.on_event("startup").
    2. urllib.parse is not present in dashboard module namespace.
    3. TestClient boots cleanly with lifespan and initializes database tables.
    """
    # Verify urllib.parse is not imported in dashboard
    assert "urllib" not in dashboard.__dict__, "urllib should not be imported in dashboard.py"

    # Verify no deprecated on_startup handlers are registered
    assert len(dashboard.app.router.on_startup) == 0, "No on_startup handlers should be registered on app.router"

    # Test lifespan initialization with an isolated db path
    test_db = str(tmp_path / "test_lifespan.db")
    monkeypatch.setattr(dashboard, "load_config", lambda: {
        "app": {"db_path": test_db}
    })

    # Boot using TestClient which triggers lifespan startup and shutdown
    with TestClient(dashboard.app) as client:
        # Tables should be initialized by lifespan startup
        assert os.path.exists(test_db), "Database file should exist after lifespan startup"
        # Test basic endpoint works
        resp = client.get("/leads")
        assert resp.status_code == 200


def test_load_config_mtime_caching(tmp_path, monkeypatch):
    """Verifies that load_config uses mtime-based in-memory caching to avoid redundant disk reads."""
    config_file = tmp_path / "config.json"
    initial_content = {"app": {"db_path": "data/initial.db"}, "queue": {"daily_queue_size": 25}}
    config_file.write_text(json.dumps(initial_content), encoding="utf-8")

    # First load: reads from disk and populates cache
    cfg1 = dashboard.load_config(config_file)
    assert cfg1["queue"]["daily_queue_size"] == 25
    assert dashboard._CONFIG_CACHE is not None

    # Second load: mtime unchanged, should return identical cached dict
    cfg2 = dashboard.load_config(config_file)
    assert cfg2 is cfg1  # Same object reference in memory

    # Modify config file on disk and advance mtime
    time.sleep(0.05)
    updated_content = {"app": {"db_path": "data/updated.db"}, "queue": {"daily_queue_size": 40}}
    config_file.write_text(json.dumps(updated_content), encoding="utf-8")
    # Touch mtime to ensure filesystem timestamp difference
    new_mtime = os.path.getmtime(str(config_file)) + 1.0
    os.utime(str(config_file), (new_mtime, new_mtime))

    cfg3 = dashboard.load_config(config_file)
    assert cfg3["queue"]["daily_queue_size"] == 40
    assert cfg3 is not cfg1
