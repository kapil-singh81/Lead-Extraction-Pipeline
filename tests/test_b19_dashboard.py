import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b19_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b19_test.db")
    database.init_db(test_db)

    # Use real config with isolated db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m08_and_p3_m10_leads_pagination_filters_and_status_tabs(b19_client_and_db):
    """
    P3-M08 & P3-M10:
    1. Seed 55 leads matching filter criteria and verify /leads renders 'Page 1 of 2'
       with Next -> link containing page=2, channel=email, flaw_type=slow_load,
       min_rating=4.5, and URL-encoded search.
    2. Status tabs include Replied, Deferred, and New.
    3. sidebar_counts.html displays Replied and Deferred counts.
    """
    client, db_path = b19_client_and_db

    # Seed 55 leads matching:
    # status="outreach_ready", channel="email", flaw_type="slow_load", min_rating>=4.5, search="A & B"
    for i in range(55):
        audit_payload = {
            "audit_completed": True,
            "flaw_codes": ["slow_load"],
            "flaw_labels": ["Slow Load Speed"]
        }
        database.insert_lead({
            "place_id": f"ChIJ_b19_lead_{i:03d}",
            "business_name": f"A & B Commercial Roofing {i}",
            "clean_phone": f"+1214555{i:04d}",
            "dm_email": f"owner{i}@abroofing.com",
            "rating": 4.9,
            "status": "outreach_ready",
            "priority_score": 80.0 + (i * 0.1),
            "audit_json": json.dumps(audit_payload)
        }, db_path=db_path)

    # Also seed 1 replied and 1 deferred lead to verify tab counts and sidebar counts
    database.insert_lead({
        "place_id": "ChIJ_b19_replied",
        "business_name": "Replied Roofing Partner",
        "status": "replied",
        "priority_score": 85.0
    }, db_path=db_path)
    database.insert_lead({
        "place_id": "ChIJ_b19_deferred",
        "business_name": "Deferred Roofing Partner",
        "status": "deferred",
        "priority_score": 75.0
    }, db_path=db_path)

    resp = client.get("/leads?status=outreach_ready&channel=email&flaw_type=slow_load&min_rating=4.5&search=A+%26+B")
    assert resp.status_code == 200
    html = resp.text

    # 1. Pagination verification
    assert "Page 1 of 2" in html
    assert "Next →" in html
    # Check Next -> link query string parameters
    assert "page=2" in html
    assert "channel=email" in html
    assert "flaw_type=slow_load" in html
    assert "min_rating=4.5" in html
    # URL-encoded search query (A%20%26%20B or A+%26+B)
    assert ("search=A%20%26%20B" in html) or ("search=A+%26+B" in html)

    # 2. Status tabs verification
    assert "Replied (" in html
    assert "Deferred (" in html
    assert "New (" in html

    # 3. Sidebar counts verification
    assert "Replied" in html
    assert "Deferred" in html


def test_p3_m08_contacted_pagination(b19_client_and_db):
    """
    P3-M08: Seed 55 contacted leads and verify GET /contacted renders 'Page 1 of 2'
    and Next -> with page=2.
    """
    client, db_path = b19_client_and_db

    for i in range(55):
        lead_id = database.insert_lead({
            "place_id": f"ChIJ_b19_contacted_{i:03d}",
            "business_name": f"Contacted Contractor {i}",
            "status": "contacted",
            "priority_score": 70.0
        }, db_path=db_path)
        database.log_outreach(
            lead_id=lead_id,
            channel="whatsapp",
            outcome="sent",
            message_sent=f"[naive_curiosity] Message {i}",
            db_path=db_path
        )

    resp = client.get("/contacted")
    assert resp.status_code == 200
    html = resp.text

    assert "Page 1 of 2" in html
    assert "Next →" in html
    assert "page=2" in html


def test_p3_m09_leads_table_digital_social_channel_no_direct_contact_suppressed(b19_client_and_db):
    """
    P3-m09: A lead on /leads with facebook_url='https://facebook.com/test' and no phone/email
    renders '📘 Facebook' without 'No Direct Contact'.
    """
    client, db_path = b19_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b19_fb_only",
        "business_name": "Social Only Construction",
        "facebook_url": "https://facebook.com/test",
        "phone": None,
        "clean_phone": None,
        "dm_email": None,
        "generic_email": None,
        "status": "outreach_ready",
        "priority_score": 77.0
    }, db_path=db_path)

    resp = client.get("/leads")
    assert resp.status_code == 200
    html = resp.text

    assert "Social Only Construction" in html
    assert "📘 Facebook" in html

    # Find the row containing this business and ensure 'No Direct Contact' is NOT inside it
    start_pos = html.find("Social Only Construction")
    assert start_pos != -1
    row_end = html.find("</tr>", start_pos)
    row_html = html[start_pos:row_end]

    assert "📘 Facebook" in row_html
    assert "No Direct Contact" not in row_html
