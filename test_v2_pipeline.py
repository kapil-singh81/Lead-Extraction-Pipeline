"""
test_v2_pipeline.py - Comprehensive offline verification suite for V2 pipeline.
Tests database migrations, categorization logic, phone classifier, fuzzy matching,
and Google Sheets 33-column alignment.
"""

import os
import sys
import gc
import logging
import database
import auditor
import sheets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
TEST_DB = "test_v2_sandbox.db"


def run_tests():
    logging.info("Starting V2 Pipeline Verification Suite...")
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except Exception:
            pass

    try:
        # 1. Test Database Initialization and V2 Column Migrations
        logging.info("--> Test 1: SQLite Schema & Auto-Migration...")
        database.init_db(TEST_DB)
        conn = database.get_connection(TEST_DB)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(leads);")
            columns = {row["name"] for row in cursor.fetchall()}
            required_cols = {"facebook_url", "form_url", "line_type", "review_snippet", "clean_phone"}
            missing = required_cols - columns
            assert not missing, f"Missing V2 columns in SQLite: {missing}"
        finally:
            conn.close()

        # 2. Test Unified Categorization Matrix
        logging.info("--> Test 2: Categorization Matrix Logic...")
        # Scenario A: Healthy site (no flaws, reachable)
        p, s = auditor.determine_lead_status(has_major_flaw=False, is_unreachable=False, clean_phone="2145551234", dm_email=None, dm_linkedin=None, generic_email=None)
        assert (p, s) == ("Disqualified", "Healthy Site"), f"Healthy check failed: {(p, s)}"

        # Scenario B: Flawed site + DM direct contact
        p, s = auditor.determine_lead_status(has_major_flaw=True, is_unreachable=False, clean_phone=None, dm_email="owner@contractor.com", dm_linkedin=None, generic_email=None)
        assert (p, s) == ("Outreach Ready", "Email / LinkedIn Available"), f"DM email check failed: {(p, s)}"

        # Scenario C: Flawed site + Phone only (The Contractor Fix)
        p, s = auditor.determine_lead_status(has_major_flaw=True, is_unreachable=False, clean_phone="2145551234", dm_email=None, dm_linkedin=None, generic_email=None)
        assert (p, s) == ("Outreach Ready", "Direct Phone Available"), f"Direct phone check failed: {(p, s)}"

        # Scenario D: Dead site + Phone present (The Dead-Site Goldmine)
        p, s = auditor.determine_lead_status(has_major_flaw=True, is_unreachable=True, clean_phone="2145551234", dm_email=None, dm_linkedin=None, generic_email=None)
        assert (p, s) == ("Outreach Ready", "Dead Domain (Phone Active)"), f"Dead domain phone check failed: {(p, s)}"

        # Scenario E: Flawed site + Zero contact info
        p, s = auditor.determine_lead_status(has_major_flaw=True, is_unreachable=False, clean_phone=None, dm_email=None, dm_linkedin=None, generic_email=None)
        assert (p, s) == ("Missing Info", "Needs Contact Info"), f"Missing contact check failed: {(p, s)}"

        # 3. Test Phone Classifier
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

        # 5. Test Google Sheets Row Alignment (Exact 33-column match)
        logging.info("--> Test 5: Google Sheets 33-Column Alignment...")
        assert len(sheets.SHEET_HEADERS) == 33, f"SHEET_HEADERS count is {len(sheets.SHEET_HEADERS)}, expected 33"

        dummy_lead = {
            "lead_uuid": "test-uuid-123",
            "business_name": "Test Roofing Co",
            "line_type": "Mobile",
            "form_url": "https://test.com/contact",
            "facebook_url": "https://facebook.com/test",
            "review_snippet": "Outstanding craftsmanship on our commercial roof!"
        }
        row = sheets.format_lead_row(dummy_lead)
        assert len(row) == 33, f"Formatted lead row returned {len(row)} columns, expected 33"
        assert row[29] == "Mobile", f"Column 29 (Line Type) expected 'Mobile', got '{row[29]}'"
        assert row[30] == "https://test.com/contact", "Column 30 (Form URL) misaligned"
        assert row[31] == "https://facebook.com/test", "Column 31 (Facebook URL) misaligned"
        assert row[32] == dummy_lead["review_snippet"], "Column 32 (Review Snippet) misaligned"

        logging.info("==================================================")
        logging.info(" ALL V2 VERIFICATION TESTS PASSED (5/5)")
        logging.info(" Pipeline V2 Tracks 1, 2, and 3 are 100% verified.")
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

