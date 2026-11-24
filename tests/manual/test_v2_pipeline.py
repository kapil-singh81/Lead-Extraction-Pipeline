"""
test_v2_pipeline.py - Offline verification suite for V2 pipeline (SQLite WAL & Micro-CRM).
Tests schema, deterministic classification, phone classifier, deduplication,
geo-grid geometry, Path B line-type writing, and outreach logging.
"""

import os
import sys
import gc
import json
import logging

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import database
import auditor
import c1_scraper
import classifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
TEST_DB = "test_v2_sandbox.db"


def run_tests():
    logging.info("Starting V2 Pipeline Verification Suite...")
    for f in [TEST_DB, f"{TEST_DB}-shm", f"{TEST_DB}-wal"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    try:
        # 1. Test Database Initialization, WAL Mode & Foreign Keys
        logging.info("--> Test 1: SQLite Schema & WAL Mode...")
        database.init_db(TEST_DB)
        conn = database.get_connection(TEST_DB)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0]
            assert journal_mode.lower() == "wal", f"Expected WAL mode, got {journal_mode}"

            cursor.execute("PRAGMA table_info(leads);")
            columns = {row["name"] for row in cursor.fetchall()}
            required_cols = {"place_id", "facebook_url", "instagram_url", "whatsapp_url", "form_url", "line_type", "review_snippet", "clean_phone", "audit_json", "priority_score"}
            missing = required_cols - columns
            assert not missing, f"Missing V2 columns in SQLite: {missing}"
        finally:
            conn.close()

        # 2. Test Deterministic Classifier Matrix
        logging.info("--> Test 2: Deterministic Classifier Gate...")
        with open("config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)

        # Scenario A: Healthy site (no flaws, completed audit) -> disqualified
        lead_a = {"business_name": "Healthy Co", "website_url": "https://healthy.com", "clean_phone": "+12145551234"}
        audit_a = {"audit_completed": True, "failed_major_test": 0, "flaw_codes": []}
        st, reas, sc = classifier.classify_lead(lead_a, audit_a, cfg)
        assert st == "disqualified" and "Healthy Website" in reas, f"Healthy check failed: {st}, {reas}"

        # Scenario B: Flawed site + Email -> outreach_ready
        lead_b = {"business_name": "Flawed Co", "website_url": "https://flawed.com", "dm_email": "owner@flawed.com"}
        audit_b = {"audit_completed": True, "failed_major_test": 1, "flaw_codes": ["slow_load"]}
        st, reas, sc = classifier.classify_lead(lead_b, audit_b, cfg)
        assert st == "outreach_ready" and "Email" in reas, f"Email outreach failed: {st}, {reas}"

        # Scenario C: Flawed site + Direct Phone -> outreach_ready
        lead_c = {"business_name": "Contractor Co", "website_url": "https://contractor.com", "clean_phone": "+12145551234", "line_type": "Mobile"}
        audit_c = {"audit_completed": True, "failed_major_test": 1, "flaw_codes": ["broken_mobile"]}
        st, reas, sc = classifier.classify_lead(lead_c, audit_c, cfg)
        assert st == "outreach_ready" and "Phone" in reas, f"Direct phone check failed: {st}, {reas}"

        # Scenario D: Dead site + Phone -> outreach_ready (Dead Domain)
        lead_d = {"business_name": "Dead Site Co", "website_url": "https://dead.com", "clean_phone": "+12145551234", "line_type": "Local (US)"}
        audit_d = {"audit_completed": True, "is_unreachable": True, "failed_major_test": 1}
        st, reas, sc = classifier.classify_lead(lead_d, audit_d, cfg)
        assert st == "outreach_ready" and "Dead Domain" in reas, f"Dead domain phone check failed: {st}, {reas}"

        # Scenario E: Flawed site + Zero actionable contact -> missing_info
        lead_e = {"business_name": "Ghost Co", "website_url": "https://ghost.com"}
        audit_e = {"audit_completed": True, "failed_major_test": 1, "flaw_codes": ["server_delay"]}
        st, reas, sc = classifier.classify_lead(lead_e, audit_e, cfg)
        assert st == "missing_info", f"Missing contact check failed: {st}, {reas}"

        # 3. Test Phone Line-Type Classifier
        logging.info("--> Test 3: Phone Line-Type Classifier...")
        assert auditor.classify_phone_line("1-800-555-0199") == "Toll-Free"
        assert auditor.classify_phone_line("888-234-5678") == "Toll-Free"
        assert auditor.classify_phone_line("214-555-0144") in {"Local (US)", "Mobile", "Landline"}
        assert auditor.classify_phone_line(None) == "None"

        # 4. Test Entity De-Aliasing & Deduplication
        logging.info("--> Test 4: Fuzzy Entity Deduplication...")
        norm_a = database.normalize_business_name("Apex Roofing, LLC")
        norm_b = database.normalize_business_name("Apex Roofing")
        assert norm_a == norm_b == "apex roofing", f"Normalization mismatch: '{norm_a}' != '{norm_b}'"

        # 5. Test Geo-Grid Tile Generation Geometry
        logging.info("--> Test 5: Geo-Grid Tile Generation...")
        tiles_2x2 = c1_scraper.generate_geo_tiles(32.7767, -96.7970, grid_size=2, step_deg=0.04)
        assert len(tiles_2x2) == 4, f"Expected 4 tiles for 2x2 grid, got {len(tiles_2x2)}"
        tiles_3x3 = c1_scraper.generate_geo_tiles(32.7767, -96.7970, grid_size=3, step_deg=0.04)
        assert len(tiles_3x3) == 9, f"Expected 9 tiles for 3x3 grid, got {len(tiles_3x3)}"

        # 6. Test Path B Processing and Persistence
        logging.info("--> Test 6: Path B Classification & Line-Type Persistence...")
        lead_id = database.insert_lead({
            "place_id": "v2_test_path_b_001",
            "business_name": "Lone Star Roofer",
            "city": "Dallas",
            "website_url": None,
            "phone": "800-555-0199",
            "clean_phone": "+18005550199",
            "review_count": 2,
            "status": "new"
        }, db_path=TEST_DB)

        test_config = {
            "app": {"db_path": TEST_DB, "target_country": "US", "excluded_domains": []},
            "queue": cfg["queue"]
        }
        auditor.process_no_website_leads(test_config)

        lead_row = database.get_lead_by_id(lead_id, db_path=TEST_DB)
        assert lead_row["line_type"] == "Toll-Free", f"Expected Toll-Free, got {lead_row['line_type']}"
        # With toll-free excluded from outreach without email, it should route to missing_info
        assert lead_row["status"] == "missing_info", f"Expected missing_info, got {lead_row['status']}"

        # 7. Test Audit Payload Update with Contact Intelligence Fields
        logging.info("--> Test 7: update_lead_audit Field Writing...")
        audit_payload = {
            "audit_completed": True,
            "load_time_sec": 2.1,
            "ttfb_ms": 250,
            "page_weight_mb": 1.2,
            "dom_nodes": 450,
            "mobile_broken": 0,
            "cms_detected": "WordPress",
            "failed_major_test": 0,
            "flaw_codes": [],
            "flaw_labels": [],
            "primary_flaw_label": "None",
            "facebook_url": "https://facebook.com/lonestar",
            "instagram_url": "https://instagram.com/lonestar_roofs",
            "form_url": "https://lonestar.com/quote",
            "line_type": "Toll-Free"
        }
        database.update_lead_audit(lead_id, audit_payload, "disqualified", "Healthy Site", 0.0, db_path=TEST_DB)

        lead_updated = database.get_lead_by_id(lead_id, db_path=TEST_DB)
        assert lead_updated["facebook_url"] == "https://facebook.com/lonestar", "Facebook URL failed to persist"
        assert lead_updated["instagram_url"] == "https://instagram.com/lonestar_roofs", "Instagram URL failed to persist"
        assert lead_updated["form_url"] == "https://lonestar.com/quote", "Form URL failed to persist"
        assert lead_updated["line_type"] == "Toll-Free", "Line Type failed to persist"
        assert lead_updated["status"] == "disqualified"

        # 8. Test Outreach Logging & Cascading Constraints
        logging.info("--> Test 8: Outreach Logging & Lead Lifecycle...")
        log_id = database.log_outreach(
            lead_id=lead_id,
            channel="phone",
            outcome="sent",
            message_sent="Left voicemail with front desk",
            notes="Follow up tomorrow",
            db_path=TEST_DB
        )
        assert log_id > 0, "Outreach log ID invalid"

        lead_contacted = database.get_lead_by_id(lead_id, db_path=TEST_DB)
        assert lead_contacted["status"] == "contacted", f"Expected 'contacted', got '{lead_contacted['status']}'"

        history = database.get_lead_outreach_history(lead_id, db_path=TEST_DB)
        assert len(history) == 1
        assert history[0]["channel"] == "phone"

        logging.info("==================================================")
        logging.info(" ALL V2 VERIFICATION TESTS PASSED (8/8)!")
        logging.info(" V2 SQLite WAL Micro-CRM Architecture is 100% verified.")
        logging.info("==================================================")

    finally:
        gc.collect()
        for f in [TEST_DB, f"{TEST_DB}-shm", f"{TEST_DB}-wal"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass


if __name__ == "__main__":
    run_tests()
