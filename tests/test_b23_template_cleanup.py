import json
import re
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import dashboard
from core import database


@pytest.fixture
def b23_client_and_db(tmp_path: Path, monkeypatch):
    test_db = str(tmp_path / "b23_test.db")
    database.init_db(test_db)

    # Use real config with isolated test db_path
    real_cfg = dashboard.load_config()
    cfg_copy = json.loads(json.dumps(real_cfg))
    cfg_copy["app"]["db_path"] = test_db
    monkeypatch.setattr(dashboard, "load_config", lambda *args, **kwargs: cfg_copy)

    client = TestClient(dashboard.app)
    return client, test_db


def test_p3_m04_no_inline_scripts_in_partials():
    """
    P3-m04: Move repeated inline partial JS out of lead_card.html & lead_drawer.html.
    Only <script type="application/json"> is permitted in partials; no executable script tags.
    """
    card_path = Path("templates/partials/lead_card.html")
    drawer_path = Path("templates/partials/lead_drawer.html")

    card_content = card_path.read_text(encoding="utf-8")
    drawer_content = drawer_path.read_text(encoding="utf-8")

    # Ensure no executable functions exist in partials
    banned_funcs = [
        "function toggleLeadEdit",
        "function toggleAddChannelPrompt",
        "function submitStatusAction",
        "function toggleDrawerEdit",
        "function toggleDrawerAddChannelPrompt",
        "_drawerSyncAttached",
    ]
    for fn in banned_funcs:
        assert fn not in card_content, f"Unexpected '{fn}' found in lead_card.html"
        assert fn not in drawer_content, f"Unexpected '{fn}' found in lead_drawer.html"

    # All script tags in partials must be type="application/json"
    for script_match in re.finditer(r"<script(.*?)>", card_content, re.IGNORECASE):
        attrs = script_match.group(1)
        assert 'type="application/json"' in attrs, f"Executable script tag in lead_card.html: {script_match.group(0)}"

    for script_match in re.finditer(r"<script(.*?)>", drawer_content, re.IGNORECASE):
        attrs = script_match.group(1)
        assert 'type="application/json"' in attrs, f"Executable script tag in lead_drawer.html: {script_match.group(0)}"


def test_p3_m12_deduplicated_drawer_js_and_loading_reset():
    """
    P3-m12: Deduplicate drawer JS into base.html & clear stale drawer content before HTMX load.
    contacted.html and leads_table.html must not contain duplicate drawer scripts.
    """
    base_content = Path("templates/base.html").read_text(encoding="utf-8")
    contacted_content = Path("templates/contacted.html").read_text(encoding="utf-8")
    table_content = Path("templates/leads_table.html").read_text(encoding="utf-8")

    # Functions required in base.html
    required_in_base = [
        "function openDrawer()",
        "function closeDrawer()",
        "function handleDrawerBackdropClick(e)",
        "function handleRowClick(e)",
        "function toggleLeadEdit(id)",
        "function toggleAddChannelPrompt(id)",
        "function submitStatusAction(leadId, stay, inGrid)",
        "function toggleDrawerEdit(id)",
        "function toggleDrawerAddChannelPrompt(id)",
    ]
    for fn in required_in_base:
        assert fn in base_content, f"Missing '{fn}' in base.html"

    # Ensure openDrawer() resets #lead-drawer-content immediately before opening
    assert "document.getElementById('lead-drawer-content')" in base_content
    assert "Loading complete lead data..." in base_content
    assert "drawer.classList.add('open')" in base_content

    # Ensure duplicate drawer client scripts are deleted from views
    assert "function openDrawer()" not in contacted_content
    assert "function closeDrawer()" not in contacted_content
    assert "function openDrawer()" not in table_content
    assert "function closeDrawer()" not in table_content


def test_p3_m11_clean_base_html_links_and_no_dead_js():
    """
    P3-m11: Remove cache-bust query param from static/style.css link and purge dead JS functions.
    """
    base_content = Path("templates/base.html").read_text(encoding="utf-8")

    # CSS link without stale cache-bust query string
    assert '<link rel="stylesheet" href="/static/style.css">' in base_content
    assert "?v=20261123_contacted_v1" not in base_content

    # Dead JS purged
    assert "function getTrackedPitch(" not in base_content
    assert "function copyPitchToClipboard(" not in base_content

    # Active pitch function remains
    assert "function copyActivePitchToClipboard(" in base_content


def test_p3_m07_niche_agnostic_wording():
    """
    P3-m07: Replace contractor / quote form labels with niche-agnostic wording.
    """
    card_content = Path("templates/partials/lead_card.html").read_text(encoding="utf-8")
    drawer_content = Path("templates/partials/lead_drawer.html").read_text(encoding="utf-8")
    contacted_content = Path("templates/contacted.html").read_text(encoding="utf-8")
    edit_form_content = Path("templates/partials/_lead_edit_form.html").read_text(encoding="utf-8") if Path("templates/partials/_lead_edit_form.html").exists() else ""
    contacts_content = Path("templates/partials/_lead_contacts.html").read_text(encoding="utf-8") if Path("templates/partials/_lead_contacts.html").exists() else ""
    full_card_content = card_content + "\n" + edit_form_content + "\n" + contacts_content
    full_drawer_content = drawer_content + "\n" + edit_form_content + "\n" + contacts_content

    contractor_pattern = re.compile(r"\bcontractor\b", re.IGNORECASE)
    quote_form_pattern = re.compile(r"quote\s+form", re.IGNORECASE)

    for name, content in [
        ("lead_card.html", full_card_content),
        ("lead_drawer.html", full_drawer_content),
        ("contacted.html", contacted_content),
    ]:
        c_matches = contractor_pattern.findall(content)
        q_matches = quote_form_pattern.findall(content)
        assert not c_matches, f"Found 'contractor' in {name}: {c_matches}"
        assert not q_matches, f"Found 'quote form' in {name}: {q_matches}"

    # Specific replacements verified
    assert "Edit Business Record:" in full_card_content
    assert "Edit business details" in full_card_content
    assert "using business phone" in full_card_content
    assert "📝 Website Contact Form" in full_card_content
    assert "prospect response" in full_card_content

    assert "Edit Business Record:" in full_drawer_content
    assert "Edit business details" in full_drawer_content
    assert "using business phone" in full_drawer_content
    assert "📝 Website Contact Form" in full_drawer_content
    assert "prospect response" in full_drawer_content

    assert "Track every business contacted across WhatsApp, phone, email, and contact forms" in contacted_content
    assert "Business & Reputation" in contacted_content
    assert "Contact Form" in contacted_content
    assert "{{ c_lead.niche | default('Local Business') }}" in contacted_content


def test_endpoint_rendering_with_b23_templates(b23_client_and_db):
    """
    Verify full rendering of views and partials using the updated templates.
    """
    client, db_path = b23_client_and_db

    audit_payload = {
        "audit_completed": True,
        "flaw_codes": ["missing_og_tags"],
        "flaw_labels": ["Missing Social Meta Tags"],
        "metrics": {"load_time_ms": 1100}
    }

    lead_id = database.insert_lead({
        "place_id": "ChIJ_b23_render_01",
        "business_name": "Horizon Plumbing HVAC",
        "clean_phone": "+12145558899",
        "city": "Plano",
        "niche": "HVAC",
        "website_url": "https://horizonplumbingtx.com",
        "rating": 4.9,
        "review_count": 64,
        "priority_score": 88.0,
        "status": "contacted",
        "audit_json": json.dumps(audit_payload)
    }, db_path=db_path)

    database.log_outreach(
        lead_id=lead_id,
        channel="form",
        outcome="sent",
        message_sent="[loss_aversion] Hey Horizon team, noticed your contact form...",
        notes="Sent via contact form",
        db_path=db_path
    )

    # 1. Queue page
    resp_queue = client.get("/")
    assert resp_queue.status_code == 200
    assert "openDrawer()" in resp_queue.text

    # 2. Leads table page
    resp_leads = client.get("/leads")
    assert resp_leads.status_code == 200
    assert "Horizon Plumbing HVAC" in resp_leads.text

    # 3. Contacted page
    resp_contacted = client.get("/contacted")
    assert resp_contacted.status_code == 200
    assert "Horizon Plumbing HVAC" in resp_contacted.text
    assert "Track every business contacted" in resp_contacted.text
    assert "Business & Reputation" in resp_contacted.text
    assert "Contact Form" in resp_contacted.text

    # 4. Partial card
    resp_card = client.get(f"/api/leads/{lead_id}/card")
    assert resp_card.status_code == 200
    assert "Edit Business Record:" in resp_card.text
    assert "Edit business details" in resp_card.text

    # 5. Partial modal drawer
    resp_modal = client.get(f"/api/leads/{lead_id}/modal")
    assert resp_modal.status_code == 200
    assert "Edit Business Record:" in resp_modal.text
    assert "📝 Website Contact Form" in resp_modal.text
