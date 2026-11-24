"""
test_brutal_pipeline.py - Exhaustive stress, regression, and integration test suite
for the redesigned SQLite WAL + Local FastAPI/HTMX Micro-CRM Lead Pipeline (V2).
"""

import gc
import json
import logging
import os
import sys
import sqlite3
from typing import Dict, Any

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from fastapi.testclient import TestClient

from core import database, classifier, auditor, c1_scraper
import dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
BRUTAL_DB = "brutal_test_sandbox.db"


def clean_db():
    gc.collect()
    for f in [BRUTAL_DB, f"{BRUTAL_DB}-shm", f"{BRUTAL_DB}-wal"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass


def test_database_brutal():
    logging.info("--> Test 1: Brutal Database Schema, WAL Mode & Foreign Keys...")
    clean_db()
    database.init_db(BRUTAL_DB)

    conn = database.get_connection(BRUTAL_DB)
    try:
        cursor = conn.cursor()

        # 1. Check WAL mode and foreign keys
        journal = cursor.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal.lower() == "wal", f"Expected WAL journal mode, got {journal}"
        fk = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1, "Foreign keys PRAGMA not enabled"

        # 2. Check 3 core tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row["name"] for row in cursor.fetchall()}
        expected_tables = {"runs", "leads", "outreach_log", "phone_prefixes"}
        assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"

        # 3. Verify leads schema columns
        cursor.execute("PRAGMA table_info(leads);")
        cols = {row["name"] for row in cursor.fetchall()}
        required_cols = {
            "id", "run_id", "place_id", "business_name", "niche", "city", "country",
            "website_url", "clean_domain", "phone", "clean_phone", "line_type",
            "facebook_url", "instagram_url", "whatsapp_url", "form_url", "dm_name", "dm_title", "dm_email",
            "dm_linkedin", "generic_email", "rating", "review_count",
            "review_snippet", "maps_url", "lead_origin", "search_query",
            "status", "status_reason", "priority_score", "audit_json",
            "manual_notes", "deferred_until", "created_at", "updated_at"
        }
        assert required_cols.issubset(cols), f"Missing columns in leads: {required_cols - cols}"
    finally:
        conn.close()

    # 4. Insert lead and test unique place_id constraint
    run_id = database.create_run("test_run", query="commercial roofer Dallas TX", db_path=BRUTAL_DB)
    lead_id = database.insert_lead({
        "run_id": run_id,
        "place_id": "0x864e9999:0x1111",
        "business_name": "Apex Commercial Roofing, LLC",
        "niche": "Roofing",
        "city": "Dallas",
        "clean_domain": "apexroofing.com",
        "clean_phone": "+12145550199",
        "status": "new"
    }, db_path=BRUTAL_DB)
    assert lead_id > 0

    # Test unique place_id violation raises IntegrityError
    try:
        database.insert_lead({
            "run_id": run_id,
            "place_id": "0x864e9999:0x1111",
            "business_name": "Apex Duplicate",
            "city": "Dallas",
            "status": "new"
        }, db_path=BRUTAL_DB)
        assert False, "Expected sqlite3.IntegrityError on duplicate place_id"
    except sqlite3.IntegrityError:
        pass

    # 5. Test deduplication across place_id, domain, phone, and fuzzy name
    assert database.lead_exists(place_id="0x864e9999:0x1111", db_path=BRUTAL_DB)
    assert database.lead_exists(clean_domain="apexroofing.com", db_path=BRUTAL_DB)
    assert database.lead_exists(clean_phone="+12145550199", db_path=BRUTAL_DB)
    assert database.lead_exists(clean_phone="214-555-0199", db_path=BRUTAL_DB)
    assert database.lead_exists(business_name="Apex Commercial Roofing Inc", city="Dallas", db_path=BRUTAL_DB)
    assert not database.lead_exists(business_name="Zenith Plumbing", city="Dallas", db_path=BRUTAL_DB)

    # Excluded domain deduplication protection (never deduplicate on facebook.com)
    assert not database.lead_exists(clean_domain="facebook.com", db_path=BRUTAL_DB)

    # 6. Test Cascading Deletes in outreach_log
    database.log_outreach(lead_id, channel="phone", notes="Initial call", db_path=BRUTAL_DB)
    history = database.get_lead_outreach_history(lead_id, db_path=BRUTAL_DB)
    assert len(history) == 1

    with database.get_connection(BRUTAL_DB) as conn:
        conn.cursor().execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.commit()

    with database.get_connection(BRUTAL_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM outreach_log WHERE lead_id = ?", (lead_id,))
        assert c.fetchone()[0] == 0, "Outreach log entry was not cascade-deleted"

    clean_db()
    logging.info("✓ Test 1: Brutal Database Suite PASSED!")


def test_classifier_brutal():
    logging.info("--> Test 2: Brutal Deterministic Classifier & Dynamic Scoring...")
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 1. Missing business name -> Disqualified
    st, rsn, sc = classifier.classify_lead({}, {"audit_completed": True}, cfg)
    assert st == "disqualified"
    assert rsn == "Missing Business Name"

    # 2. Uncompleted audit -> Must remain 'new' (never prematurely evaluated)
    st, rsn, sc = classifier.classify_lead({"business_name": "Acme Inc"}, {}, cfg)
    assert st == "new"
    assert rsn == "Pending Audit Completion"

    # 3. Healthy site (0 flaws after complete audit) -> Disqualified
    healthy_audit = {
        "audit_completed": True,
        "has_website": True,
        "is_unreachable": False,
        "failed_major_test": 0,
        "flaw_codes": [],
        "flaw_labels": []
    }
    st, rsn, sc = classifier.classify_lead({
        "business_name": "Perfect Site LLC",
        "website_url": "https://perfect.com",
        "clean_phone": "+12145550199"
    }, healthy_audit, cfg)
    assert st == "disqualified"
    assert "Healthy Website" in rsn

    # 4. Flawed site with actionable contact -> Outreach Ready + Priority Score
    flawed_audit = {
        "audit_completed": True,
        "has_website": True,
        "is_unreachable": False,
        "failed_major_test": 1,
        "flaw_codes": ["broken_mobile", "missing_lead_capture"],
        "flaw_labels": ["Broken Mobile Layout", "Missing Form"]
    }
    lead_actionable = {
        "business_name": "Flawed Roofer",
        "website_url": "https://flawed.com",
        "clean_phone": "+12145550199",
        "dm_email": "owner@flawed.com",
        "line_type": "Mobile",
        "review_count": 25
    }
    st, rsn, sc = classifier.classify_lead(lead_actionable, flawed_audit, cfg)
    assert st == "outreach_ready"
    assert sc >= 8.0  # 3.0 (broken mobile) + 2.5 (missing capture) + 2.0 (email) + 1.5 (mobile) + reviews

    # 5. Flawed site with missing contact -> Missing Info
    lead_no_contact = {
        "business_name": "Flawed No Contact",
        "website_url": "https://flawed.com"
    }
    st, rsn, sc = classifier.classify_lead(lead_no_contact, flawed_audit, cfg)
    assert st == "missing_info"

    # 6. Toll-free phone filtering (excluded from outreach_ready)
    lead_toll_free = {
        "business_name": "Toll Free Only",
        "website_url": "https://flawed.com",
        "clean_phone": "+18005550199",
        "line_type": "Toll-Free"
    }
    st, rsn, sc = classifier.classify_lead(lead_toll_free, flawed_audit, cfg)
    assert st == "missing_info", f"Expected missing_info for 800-only lead, got {st}"

    # 7. Path B (No website) with active phone -> Outreach Ready
    no_web_audit = {
        "audit_completed": True,
        "has_website": False,
        "is_unreachable": False,
        "failed_major_test": 1,
        "flaw_codes": ["no_website"]
    }
    lead_no_web = {
        "business_name": "Local Masonry",
        "clean_phone": "+12145550199",
        "website_url": None
    }
    st, rsn, sc = classifier.classify_lead(lead_no_web, no_web_audit, cfg)
    assert st == "outreach_ready"
    assert "No Website" in rsn
    assert sc == 4.0

    # 8. Unreachable site with active phone -> Outreach Ready
    dead_audit = {
        "audit_completed": True,
        "has_website": True,
        "is_unreachable": True,
        "failed_major_test": 1,
        "flaw_codes": ["server_delay"]
    }
    lead_dead = {
        "business_name": "Dead Domain Roofer",
        "clean_phone": "+12145550199",
        "website_url": "https://deaddomain.com"
    }
    st, rsn, sc = classifier.classify_lead(lead_dead, dead_audit, cfg)
    assert st == "outreach_ready"
    assert "Dead Domain" in rsn
    assert sc == 5.0

    logging.info("✓ Test 2: Brutal Classifier Suite PASSED!")


def test_scraper_and_auditor_brutal():
    logging.info("--> Test 3: Brutal Scraper Extraction & Auditor Helpers...")
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 1. Deterministic place_id guarantee
    pid_hex = c1_scraper.get_or_derive_place_id("https://maps.google.com/?cid=123!1s0x864e:0x1111", "Apex", "Dallas", "+12145550199")
    assert pid_hex == "0x864e:0x1111"

    pid_gen1 = c1_scraper.get_or_derive_place_id(None, "Summit Roofing", "Dallas", "+12145550199")
    pid_gen2 = c1_scraper.get_or_derive_place_id(None, "Summit Roofing", "Dallas", "+12145550199")
    assert pid_gen1.startswith("gen:")
    assert pid_gen1 == pid_gen2  # Deterministic hash equality

    # 2. Clean website URL with redirect stripping and excluded_domains filter
    ex_domains = cfg["app"]["excluded_domains"]
    url1, dom1 = c1_scraper.clean_website_url("https://www.google.com/url?q=https://acmeroofing.com/services&sa=D", excluded_domains=ex_domains)
    assert url1 == "https://acmeroofing.com/services"
    assert dom1 == "acmeroofing.com"

    url_fb, dom_fb = c1_scraper.clean_website_url("https://www.facebook.com/pages/acmeroofing", excluded_domains=ex_domains)
    assert url_fb is None and dom_fb is None  # Excluded domain cleanly rejected

    # 3. Auditor CMS detection
    assert auditor.detect_cms('<div class="elementor-widget"></div>') == "WordPress (Elementor)"
    assert auditor.detect_cms('<script src="https://static.wixstatic.com/test.js"></script>') == "Wix"

    # 4. Auditor Email Extraction & Noise Filtering
    dm_emails, gen_emails = auditor.extract_emails(
        "Contact info@roofing.com or owner mark@roofing.com or dummy@example.com or 463991d7bca24f02a0baf16f9e6eda73@sentry.wixpress",
        cfg["auditor"]["generic_email_prefixes"]
    )
    assert "mark@roofing.com" in dm_emails
    assert "info@roofing.com" in gen_emails
    assert "dummy@example.com" not in dm_emails and "dummy@example.com" not in gen_emails
    assert not any("sentry" in e for e in dm_emails + gen_emails)

    # 5. Auditor Multi-Channel Social Media Extraction
    test_links = [
        "https://www.facebook.com/sharer/sharer.php?u=https://acme.com",
        "https://www.facebook.com/acmecommercialroofing/",
        "https://www.instagram.com/acme_roofs/?hl=en",
        "https://www.instagram.com/p/C3m2Xyz123/",
        "https://www.linkedin.com/company/acme-roofing-tx",
        "https://x.com/acmeroofs",
        "https://twitter.com/intent/tweet?text=hello",
        "https://www.youtube.com/@acmeroofingchannel"
    ]
    socials = auditor.extract_social_links(test_links)
    assert socials["facebook_url"] == "https://www.facebook.com/acmecommercialroofing"
    assert socials["instagram_url"] == "https://www.instagram.com/acme_roofs"
    assert socials["linkedin_url"] == "https://www.linkedin.com/company/acme-roofing-tx"
    assert socials["twitter_url"] == "https://x.com/acmeroofs"
    assert socials["youtube_url"] == "https://www.youtube.com/@acmeroofingchannel"

    # 6. Auditor Subpage Categorization
    homepage_links = [
        {"href": "https://acmeroofing.com/contact-us", "text": "Contact Us"},
        {"href": "https://acmeroofing.com/our-projects", "text": "Commercial Projects Gallery"},
        {"href": "https://acmeroofing.com/about-our-team", "text": "About Company"},
        {"href": "https://acmeroofing.com/reviews", "text": "Customer Reviews"},
        {"href": "https://google.com/maps", "text": "External Map"}
    ]
    subpages = auditor.discover_key_subpages("https://acmeroofing.com", homepage_links)
    assert subpages.get("contact") == "https://acmeroofing.com/contact-us"
    assert subpages.get("gallery") == "https://acmeroofing.com/our-projects"
    assert subpages.get("about") == "https://acmeroofing.com/about-our-team"
    assert subpages.get("reviews") == "https://acmeroofing.com/reviews"

    # 7. Auditor Phone Line Classification
    assert auditor.classify_phone_line("+18005550199") == "Toll-Free"
    assert auditor.classify_phone_line("+12145550199") == "Local (US)"

    logging.info("✓ Test 3: Brutal Scraper & Auditor Suite PASSED!")


def test_dashboard_cockpit_brutal():
    logging.info("--> Test 4: Brutal Dashboard Verification Cockpit & Progress Tracking...")
    clean_db()
    database.init_db(BRUTAL_DB)

    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["app"]["db_path"] = BRUTAL_DB
    orig_load_config = dashboard.load_config
    dashboard.load_config = lambda: cfg

    try:
        client = TestClient(dashboard.app)

        # 1. Local offline HTMX asset verification
        assert os.path.exists("static/htmx.min.js"), "Local static/htmx.min.js bundle missing"
        assert os.path.exists("static/style.css"), "static/style.css missing"

        resp_base = client.get("/")
        assert resp_base.status_code == 200
        assert "/static/htmx.min.js" in resp_base.text  # Verifies zero external CDN dependence

        # 2. Seed Lead and test Action Logging & Progress Tracker
        run_id = database.create_run("test_run", query="roofing", db_path=BRUTAL_DB)
        lead_id = database.insert_lead({
            "run_id": run_id,
            "place_id": "gen:cockpit_lead_1",
            "business_name": "Lone Star Commercial Roofing",
            "city": "Fort Worth",
            "country": "US",
            "website_url": "https://lonestarroofing.com",
            "clean_domain": "lonestarroofing.com",
            "clean_phone": "+18175550199",
            "line_type": "Mobile",
            "rating": 4.9,
            "review_count": 45,
            "status": "outreach_ready",
            "priority_score": 90.0,
            "audit_json": json.dumps({
                "audit_completed": True,
                "flaw_codes": ["broken_mobile"],
                "flaw_labels": ["Broken Mobile Layout (Horizontal Overflow)"]
            })
        }, db_path=BRUTAL_DB)

        # Check top card rendering
        resp_queue = client.get("/")
        assert resp_queue.status_code == 200
        assert "Lone Star Commercial Roofing" in resp_queue.text
        assert "90.0" in resp_queue.text
        assert "0 / 35 Reviewed Today" in resp_queue.text

        # Action: Log outreach via Phone
        resp_act = client.post(
            f"/api/leads/{lead_id}/action",
            data={"action": "contacted", "channel": "phone", "notes": "Left voicemail with receptionist"}
        )
        assert resp_act.status_code == 200
        assert "Daily Queue Completed" in resp_act.text  # Only 1 lead was queued
        assert "1 / 35 Reviewed Today" in resp_act.text  # Progress incremented accurately

        # Verify outreach_log in SQLite
        history = database.get_lead_outreach_history(lead_id, db_path=BRUTAL_DB)
        assert len(history) == 1
        assert history[0]["channel"] == "phone"

        # Master table filter test
        resp_tbl = client.get("/leads?search=Lone+Star")
        assert resp_tbl.status_code == 200
        assert "Lone Star Commercial Roofing" in resp_tbl.text
    finally:
        dashboard.load_config = orig_load_config
        clean_db()

    logging.info("✓ Test 4: Brutal Dashboard Cockpit Suite PASSED!")


def test_whatsapp_sync_brutal():
    logging.info("--> Test 5: Synchronized WhatsApp Intelligence & 1-Click Cockpit Suite...")
    clean_db()
    database.init_db(BRUTAL_DB)

    # 1. Test extraction: direct link
    links_direct = ["https://wa.me/12145550188?text=Hello", "https://roofing.com/about"]
    wa_url, is_active, src = auditor.extract_whatsapp_info(links_direct, "<html></html>", None, "Unknown")
    assert is_active and src == "website_link" and "12145550188" in wa_url

    # 2. Test extraction: widget DOM signature
    html_widget = "<div id='joinchat' class='joinchat-button'>Chat with us</div>"
    wa_url, is_active, src = auditor.extract_whatsapp_info([], html_widget, "+1 (214) 555-0199", "Landline")
    assert is_active and src == "website_widget" and "12145550199" in wa_url

    # 3. Test extraction: mobile line fallback
    wa_url, is_active, src = auditor.extract_whatsapp_info([], "<html><body>Normal Site</body></html>", "+1 (214) 555-0199", "Mobile")
    assert not is_active and src == "mobile_registry" and "12145550199" in wa_url

    # 4. Test extraction: landline with no widget -> None
    wa_url, is_active, src = auditor.extract_whatsapp_info([], "<html><body>Normal Site</body></html>", "+1 (214) 555-0199", "Local (US)")
    assert not is_active and src == "none" and wa_url is None

    # 5. Test Priority scoring and classification
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    audit_flawed = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": ["broken_mobile"],
        "failed_major_test": 1,
        "whatsapp_url": "https://wa.me/12145550199",
        "is_whatsapp_active": True
    }
    lead_wa = {
        "business_name": "Lone Star WhatsApp Roofing",
        "website_url": "https://lonestar.com",
        "clean_phone": "+12145550199",
        "line_type": "Mobile",
        "whatsapp_url": "https://wa.me/12145550199",
        "audit_json": json.dumps(audit_flawed)
    }
    st, reason, score = classifier.classify_lead(lead_wa, audit_flawed, cfg)
    assert st == "outreach_ready"
    assert "WhatsApp" in reason or "Phone" in reason
    # Score should include broken_mobile(3.0) + mobile_phone_bonus(1.5) + whatsapp_bonus(1.5) = 6.0
    assert score >= 6.0

    # 6. Test Cockpit UI unpack_audit & 1-Click Launch URL
    lead_unpacked = dashboard.unpack_audit(lead_wa)
    assert lead_unpacked.get("whatsapp_launch_url") is not None
    assert "wa.me/12145550199" in lead_unpacked["whatsapp_launch_url"]
    assert "text=" in lead_unpacked["whatsapp_launch_url"]

    # 7. Test Cockpit HTMX Action: Channel == "whatsapp"
    run_id = database.create_run("wa_test_run", query="roofer", db_path=BRUTAL_DB)
    lead_id = database.insert_lead({
        "run_id": run_id,
        "place_id": "0xwa:0x1",
        "business_name": "Lone Star WhatsApp Roofing",
        "clean_phone": "+12145550199",
        "whatsapp_url": "https://wa.me/12145550199",
        "status": "outreach_ready",
        "priority_score": 6.0
    }, db_path=BRUTAL_DB)

    orig_load_config = dashboard.load_config
    cfg["app"]["db_path"] = BRUTAL_DB
    dashboard.load_config = lambda: cfg
    try:
        client = TestClient(dashboard.app)
        resp = client.post(
            f"/api/leads/{lead_id}/action",
            data={"action": "contacted", "channel": "whatsapp"}
        )
        assert resp.status_code == 200

        # Verify outreach_log recorded channel="whatsapp"
        history = database.get_lead_outreach_history(lead_id, db_path=BRUTAL_DB)
        assert len(history) == 1
        assert history[0]["channel"] == "whatsapp"
        assert any(k in history[0]["message_sent"] for k in [
            "naive_curiosity", "Curiosity", "loss_aversion", "Loss Aversion",
            "no_oriented", "No-Oriented", "reputation_dissonance", "Reputation",
            "accusation_audit", "Accusation", "local_contrast", "Local Contrast",
            "gatekeeper", "Gatekeeper", "reciprocity", "Reciprocity", "WhatsApp"
        ])
    finally:
        dashboard.load_config = orig_load_config
        clean_db()
    logging.info("✓ Test 5: Synchronized WhatsApp Intelligence & 1-Click Cockpit Suite PASSED!")


def run_all():
    print("=" * 65)
    print(" EXECUTING MASTER BRUTAL PIPELINE VERIFICATION SUITE")
    print("=" * 65)
    try:
        test_database_brutal()
        test_classifier_brutal()
        test_scraper_and_auditor_brutal()
        test_dashboard_cockpit_brutal()
        test_whatsapp_sync_brutal()
        print("=" * 65)
        print(" ALL 5 BRUTAL PIPELINE TEST SUITES PASSED (100% GREEN)!")
        print(" SQLite WAL + Micro-CRM Storage Architecture is Rock Solid.")
        print("=" * 65)
    finally:
        clean_db()


if __name__ == "__main__":
    run_all()
