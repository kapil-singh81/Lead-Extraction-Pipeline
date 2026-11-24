import os
import sys
import gc
import json
import sqlite3

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from core import database

TEST_DB = "test_redesign_db.db"


def clean():
    gc.collect()
    for f in [TEST_DB, f"{TEST_DB}-wal", f"{TEST_DB}-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass


def test_database():
    clean()
    try:
        print("--> Testing init_db and PRAGMA settings...")
        database.init_db(TEST_DB)
        conn = database.get_connection(TEST_DB)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys;")
            assert cursor.fetchone()[0] == 1, "Foreign keys not enabled"
            cursor.execute("PRAGMA journal_mode;")
            assert cursor.fetchone()[0].lower() == "wal", "WAL mode not enabled"

            # Check tables and columns
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {r[0] for r in cursor.fetchall()}
            assert {"runs", "leads", "outreach_log", "phone_prefixes"}.issubset(tables), f"Missing tables: {tables}"

            cursor.execute("PRAGMA table_info(leads);")
            cols = {r["name"] for r in cursor.fetchall()}
            assert "whatsapp_url" in cols, "whatsapp_url column missing from leads table"
        finally:
            conn.close()

        print("--> Testing runs & lead creation...")
        run_id = database.create_run(run_type="local_scrape", query="commercial roofer Dallas TX", db_path=TEST_DB)
        assert run_id > 0

        lead_id = database.insert_lead({
            "run_id": run_id,
            "place_id": "0x864e9999:0x1111",
            "business_name": "Apex Roofing LLC",
            "city": "Dallas",
            "clean_phone": "+12145550199",
            "website_url": "https://apexroofing.com",
            "clean_domain": "apexroofing.com",
            "whatsapp_url": "https://wa.me/12145550199",
            "status": "new"
        }, db_path=TEST_DB)
        assert lead_id > 0

        print("--> Testing unique place_id constraint...")
        try:
            database.insert_lead({
                "place_id": "0x864e9999:0x1111",
                "business_name": "Apex Clone",
                "status": "new"
            }, db_path=TEST_DB)
            assert False, "Duplicate place_id was not rejected"
        except sqlite3.IntegrityError:
            pass  # Expected

        print("--> Testing lead_exists checks...")
        assert database.lead_exists(place_id="0x864e9999:0x1111", db_path=TEST_DB)
        assert database.lead_exists(clean_domain="apexroofing.com", db_path=TEST_DB)
        assert database.lead_exists(clean_phone="214-555-0199", db_path=TEST_DB)
        assert database.lead_exists(business_name="Apex Roofing Inc", city="Dallas", db_path=TEST_DB)
        assert not database.lead_exists(business_name="Unrelated Plumbing", city="Dallas", db_path=TEST_DB)

        print("--> Testing update_lead_audit with audit_json...")
        audit_payload = {
            "audit_completed": True,
            "flaw_codes": ["broken_mobile", "missing_lead_capture"],
            "flaw_labels": ["Broken Mobile Layout", "Missing Lead Capture"],
            "metrics": {"ttfb_ms": 320, "dom_nodes": 450}
        }
        database.update_lead_audit(
            lead_id=lead_id,
            audit_data=audit_payload,
            status="outreach_ready",
            status_reason="Flawed Site (Phone Available)",
            priority_score=5.5,
            db_path=TEST_DB
        )

        conn = database.get_connection(TEST_DB)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status, priority_score, audit_json FROM leads WHERE id = ?", (lead_id,))
            row = cursor.fetchone()
            assert row["status"] == "outreach_ready"
            assert row["priority_score"] == 5.5
            saved_audit = json.loads(row["audit_json"])
            assert saved_audit["flaw_codes"] == ["broken_mobile", "missing_lead_capture"]
        finally:
            conn.close()

        print("--> Testing get_daily_queue & queue progress tracking...")
        q = database.get_daily_queue(daily_queue_size=35, db_path=TEST_DB)
        assert q["done_today"] == 0
        assert q["remaining_quota"] == 35
        assert len(q["leads"]) == 1

        print("--> Testing log_outreach & status transition...")
        log_id = database.log_outreach(
            lead_id=lead_id,
            channel="whatsapp",
            outcome="sent",
            message_sent="WhatsApp pitch sent via 1-click cockpit launcher",
            notes="Follow up tomorrow",
            db_path=TEST_DB
        )
        assert log_id > 0

        # Check lead status updated to contacted
        stats = database.get_pipeline_stats(TEST_DB)
        assert stats.get("contacted") == 1
        assert stats.get("total") == 1

        # Check queue progress now shows done_today = 1
        q_after = database.get_daily_queue(daily_queue_size=35, db_path=TEST_DB)
        assert q_after["done_today"] == 1
        assert q_after["remaining_quota"] == 34
        assert len(q_after["leads"]) == 0  # 0 remaining because the only lead is now contacted!

        print("--> Testing get_leads_for_reaudit...")
        reaudit_leads = database.get_leads_for_reaudit(TEST_DB)
        assert len(reaudit_leads) == 1
        assert reaudit_leads[0]["id"] == lead_id

        print("--> Testing cascade delete on foreign keys...")
        conn = database.get_connection(TEST_DB)
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            conn.commit()
            cursor.execute("SELECT COUNT(*) FROM outreach_log WHERE lead_id = ?", (lead_id,))
            assert cursor.fetchone()[0] == 0, "Cascading delete to outreach_log failed"
        finally:
            conn.close()

        print("\nALL DATABASE TESTS PASSED (100%)!")

    finally:
        clean()


if __name__ == "__main__":
    test_database()
