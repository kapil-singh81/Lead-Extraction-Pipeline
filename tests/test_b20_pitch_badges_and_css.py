import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b20_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b20_test.db")
    database.init_db(test_db)

    # Use real config with isolated test db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m07_local_contrast_badge_and_snippet_display(b20_client_and_db):
    """
    P3-M07:
    Logging '[local_contrast] Way stronger reputation than half the Dallas listings...'
    renders <span class="pitch-badge local-contrast">📍 Local Contrast</span> (NOT reputation-dissonance)
    AND displays the sent body text in both GET /contacted and GET /api/leads/{id}/modal.
    """
    client, db_path = b20_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b20_contrast_01",
        "business_name": "Lone Star Premier Roofing",
        "clean_phone": "+12145551234",
        "city": "Dallas",
        "rating": 4.8,
        "review_count": 62,
        "priority_score": 85.0
    }, db_path=db_path)

    msg = "[local_contrast] Way stronger reputation than half the Dallas listings..."
    database.log_outreach(
        lead_id=lead_id,
        channel="email",
        outcome="sent",
        message_sent=msg,
        notes="First touch sent via custom email template",
        db_path=db_path
    )

    # 1. GET /contacted (Table View)
    resp_contacted = client.get("/contacted?view=table")
    assert resp_contacted.status_code == 200
    html_contacted = resp_contacted.text

    assert '<span class="pitch-badge local-contrast">📍 Local Contrast</span>' in html_contacted
    assert "reputation-dissonance" not in html_contacted
    assert '<div class="contact-msg-snippet"' in html_contacted
    assert "Way stronger reputation than half the Dallas listings..." in html_contacted

    # 2. GET /api/leads/{id}/modal (Slide-over Drawer)
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    html_modal = resp_modal.text

    assert '<span class="pitch-badge local-contrast">📍 Local Contrast</span>' in html_modal
    assert "reputation-dissonance" not in html_modal
    assert "Way stronger reputation than half the Dallas listings..." in html_modal
    assert "First touch sent via custom email template" in html_modal


def test_p3_m07_legacy_unbracketed_message_clean_rendering(b20_client_and_db):
    """
    P3-M07:
    Logging a legacy unbracketed message 'Custom note without tag' renders the message
    text cleanly without misclassifying it as a strategy badge.
    """
    client, db_path = b20_client_and_db

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b20_legacy_02",
        "business_name": "Apex Plumbing Pros",
        "clean_phone": "+12145559876",
        "city": "Fort Worth",
        "rating": 4.5,
        "review_count": 30,
        "priority_score": 75.0
    }, db_path=db_path)

    legacy_msg = "Custom note without tag"
    database.log_outreach(
        lead_id=lead_id,
        channel="phone",
        outcome="sent",
        message_sent=legacy_msg,
        notes="Called owner direct line",
        db_path=db_path
    )

    # 1. GET /contacted (Table View)
    resp_contacted = client.get("/contacted?view=table")
    assert resp_contacted.status_code == 200
    html_contacted = resp_contacted.text

    # Message text is displayed in snippet
    assert "Custom note without tag" in html_contacted
    # No pitch-badge should be rendered for this unbracketed message
    assert '<span class="pitch-badge' not in html_contacted

    # 2. GET /api/leads/{id}/modal (Slide-over Drawer)
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    html_modal = resp_modal.text

    assert "Custom note without tag" in html_modal
    assert '<span class="pitch-badge' not in html_modal


def test_p3_m07_all_strategy_tags_mapping(b20_client_and_db):
    """
    Verify all 8 core strategy tags plus custom tag map to their correct badge classes.
    """
    client, db_path = b20_client_and_db

    tags_and_expected = [
        ("naive_curiosity", "curiosity", "🔍 Curiosity"),
        ("loss_aversion", "loss-aversion", "⚠️ Loss Aversion"),
        ("accusation_audit", "accusation", "🛡️ Accusation"),
        ("no_oriented", "no-oriented", "🧠 No-Oriented"),
        ("no-oriented", "no-oriented", "🧠 No-Oriented"),
        ("local_contrast", "local-contrast", "📍 Local Contrast"),
        ("reputation_dissonance", "reputation-dissonance", "⭐ Reputation"),
        ("gatekeeper", "gatekeeper", "🚪 Gatekeeper"),
        ("reciprocity", "reciprocity", "🎁 Reciprocity"),
        ("vip_intro", "default", "📋 Vip Intro"),
    ]

    for idx, (tag, expected_cls, expected_label) in enumerate(tags_and_expected):
        lead_id = database.insert_lead({
            "place_id": f"ChIJ_b20_strat_{idx:02d}",
            "business_name": f"Strategy Contractor {idx}",
            "priority_score": 80.0
        }, db_path=db_path)

        msg = f"[{tag}] Custom body text for strategy {idx}"
        database.log_outreach(
            lead_id=lead_id,
            channel="email",
            outcome="sent",
            message_sent=msg,
            db_path=db_path
        )

        resp_modal = client.get(f"/api/leads/{lead_id}/modal")
        assert resp_modal.status_code == 200
        html = resp_modal.text

        expected_badge = f'<span class="pitch-badge {expected_cls}">{expected_label}</span>'
        assert expected_badge in html, f"Expected {expected_badge} for tag {tag}"
        assert f"Custom body text for strategy {idx}" in html


def test_p2_m11_and_p3_m01_css_definitions():
    """
    P2-m11 & P3-m01:
    1. static/style.css defines .channel-pill.
    2. static/style.css defines .pitch-badge for curiosity, accusation, local-contrast,
       gatekeeper, and reciprocity (both light mode and html.dark rules).
    """
    css_path = Path("static/style.css")
    assert css_path.exists(), "static/style.css must exist"
    css_content = css_path.read_text(encoding="utf-8")

    # 1. .channel-pill definition
    assert ".channel-pill {" in css_content
    assert "display: inline-flex;" in css_content
    assert "border-radius: var(--radius-pill);" in css_content

    # 2. 5 new pitch badge classes in light mode
    assert ".pitch-badge.curiosity {" in css_content
    assert ".pitch-badge.accusation {" in css_content
    assert ".pitch-badge.local-contrast {" in css_content
    assert ".pitch-badge.gatekeeper {" in css_content
    assert ".pitch-badge.reciprocity {" in css_content

    # 3. 5 new pitch badge classes in dark mode (html.dark)
    assert "html.dark .pitch-badge.curiosity {" in css_content
    assert "html.dark .pitch-badge.accusation {" in css_content
    assert "html.dark .pitch-badge.local-contrast {" in css_content
    assert "html.dark .pitch-badge.gatekeeper {" in css_content
    assert "html.dark .pitch-badge.reciprocity {" in css_content
