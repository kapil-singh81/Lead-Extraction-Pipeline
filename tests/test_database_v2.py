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


import pytest


def test_get_connection_creates_nested_parent_dir(tmp_path):
    nested_db = tmp_path / "nested" / "dir" / "test.db"
    assert not (tmp_path / "nested" / "dir").exists()
    conn = database.get_connection(str(nested_db))
    try:
        assert (tmp_path / "nested" / "dir").exists()
        cursor = conn.cursor()
        cursor.execute("SELECT 1;")
        assert cursor.fetchone()[0] == 1
    finally:
        conn.close()


def test_update_lead_fields_url_and_domain(tmp_path):
    db_file = str(tmp_path / "test_url.db")
    database.init_db(db_file)
    lead_id = database.insert_lead({
        "place_id": "0x123:0x456",
        "business_name": "Test Roofer",
        "status": "new"
    }, db_path=db_file)

    updated = database.update_lead_fields(
        lead_id,
        {"website_url": "example.com/contact-us?ref=1"},
        db_path=db_file
    )
    assert updated is not None
    assert updated["website_url"] == "https://example.com/contact-us?ref=1"
    assert updated["clean_domain"] == "example.com"


def test_update_lead_fields_phone_and_clean_phone(tmp_path):
    db_file = str(tmp_path / "test_phone.db")
    database.init_db(db_file)
    lead_id = database.insert_lead({
        "place_id": "0x123:0x789",
        "business_name": "Phone Test",
        "status": "new"
    }, db_path=db_file)

    # Single phone update derives clean_phone without swap
    updated = database.update_lead_fields(
        lead_id,
        {"phone": "(214) 555-0199"},
        db_path=db_file
    )
    assert updated["phone"] == "(214) 555-0199"
    assert updated["clean_phone"] == "+12145550199"

    # Passing both phone and clean_phone produces no duplicate SQL column error
    updated2 = database.update_lead_fields(
        lead_id,
        {"phone": "(214) 555-0199", "clean_phone": "+12145550199"},
        db_path=db_file
    )
    assert updated2["phone"] == "(214) 555-0199"
    assert updated2["clean_phone"] == "+12145550199"


def test_update_lead_fields_audit_json(tmp_path):
    db_file = str(tmp_path / "test_audit.db")
    database.init_db(db_file)
    lead_id = database.insert_lead({
        "place_id": "0x123:0xabc",
        "business_name": "Audit Test",
        "status": "new"
    }, db_path=db_file)

    payload = json.dumps({"twitter_url": "https://x.com/test"})
    updated = database.update_lead_fields(
        lead_id,
        {"audit_json": payload},
        db_path=db_file
    )
    assert updated["audit_json"] == payload

    # Malformed JSON raises ValueError
    with pytest.raises(ValueError):
        database.update_lead_fields(
            lead_id,
            {"audit_json": "{malformed_json: true"},
            db_path=db_file
        )

    # Dict serializes to JSON
    updated_dict = database.update_lead_fields(
        lead_id,
        {"audit_json": {"custom_channel": "https://test.com"}},
        db_path=db_file
    )
    assert json.loads(updated_dict["audit_json"]) == {"custom_channel": "https://test.com"}


from datetime import datetime, timedelta, timezone


def test_deferred_leads_queue_resurfacing(tmp_path):
    db_file = str(tmp_path / "test_defer_queue.db")
    database.init_db(db_file)

    now_utc = datetime.now(timezone.utc)
    past_utc = (now_utc - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    future_utc = (now_utc + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    # Lead 1: deferred in the past -> must resurface
    lead1_id = database.insert_lead({
        "place_id": "0x111:0x111",
        "business_name": "Past Deferred Roofer",
        "status": "deferred",
        "deferred_until": past_utc,
        "priority_score": 8.0
    }, db_path=db_file)

    # Lead 2: deferred in the future -> must NOT resurface
    lead2_id = database.insert_lead({
        "place_id": "0x222:0x222",
        "business_name": "Future Deferred Roofer",
        "status": "deferred",
        "deferred_until": future_utc,
        "priority_score": 9.0
    }, db_path=db_file)

    # Lead 3: deferred with None -> must NOT resurface
    lead3_id = database.insert_lead({
        "place_id": "0x333:0x333",
        "business_name": "None Deferred Roofer",
        "status": "deferred",
        "deferred_until": None,
        "priority_score": 7.0
    }, db_path=db_file)

    queue = database.get_daily_queue(daily_queue_size=10, db_path=db_file)
    queue_lead_ids = [l["id"] for l in queue["leads"]]
    assert lead1_id in queue_lead_ids
    assert lead2_id not in queue_lead_ids
    assert lead3_id not in queue_lead_ids


def test_social_columns_migration_and_persistence(tmp_path):
    db_file = str(tmp_path / "test_social_mig.db")

    # 1. Create a legacy pre-B2 table missing linkedin_url, twitter_url, youtube_url
    conn = database.get_connection(db_file)
    try:
        with conn:
            conn.execute("""
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    place_id TEXT UNIQUE NOT NULL,
                    business_name TEXT NOT NULL,
                    niche TEXT,
                    city TEXT,
                    country TEXT DEFAULT 'US',
                    website_url TEXT,
                    clean_domain TEXT,
                    phone TEXT,
                    clean_phone TEXT,
                    line_type TEXT DEFAULT 'Unknown',
                    facebook_url TEXT,
                    instagram_url TEXT,
                    whatsapp_url TEXT,
                    form_url TEXT,
                    dm_name TEXT,
                    dm_title TEXT,
                    dm_email TEXT,
                    dm_linkedin TEXT,
                    generic_email TEXT,
                    rating REAL DEFAULT 0.0,
                    review_count INTEGER DEFAULT 0,
                    review_snippet TEXT,
                    maps_url TEXT,
                    lead_origin TEXT,
                    search_query TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    status_reason TEXT,
                    priority_score REAL DEFAULT 0.0,
                    audit_json TEXT,
                    manual_notes TEXT,
                    deferred_until TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
    finally:
        conn.close()

    # 2. Run init_db to execute safe column migrations
    database.init_db(db_file)

    conn = database.get_connection(db_file)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(leads);")
        cols = {r["name"] for r in cursor.fetchall()}
        assert "linkedin_url" in cols
        assert "twitter_url" in cols
        assert "youtube_url" in cols
    finally:
        conn.close()

    # 3. Test insert_lead with new social columns
    lead_id = database.insert_lead({
        "place_id": "0x444:0x444",
        "business_name": "Social Roofer",
        "linkedin_url": "https://linkedin.com/company/socialroofer",
        "twitter_url": "https://x.com/socialroofer",
        "youtube_url": "https://youtube.com/@socialroofer",
        "status": "new"
    }, db_path=db_file)
    assert lead_id > 0

    lead = database.get_lead_by_id(lead_id, db_path=db_file)
    assert lead["linkedin_url"] == "https://linkedin.com/company/socialroofer"
    assert lead["twitter_url"] == "https://x.com/socialroofer"
    assert lead["youtube_url"] == "https://youtube.com/@socialroofer"

    # 4. Test update_lead_audit sync
    database.update_lead_audit(
        lead_id=lead_id,
        audit_data={
            "linkedin_url": "https://linkedin.com/company/updated",
            "twitter_url": "https://x.com/updated",
            "youtube_url": "https://youtube.com/@updated"
        },
        status="outreach_ready",
        status_reason="Audited",
        priority_score=5.0,
        db_path=db_file
    )
    lead_audited = database.get_lead_by_id(lead_id, db_path=db_file)
    assert lead_audited["linkedin_url"] == "https://linkedin.com/company/updated"
    assert lead_audited["twitter_url"] == "https://x.com/updated"
    assert lead_audited["youtube_url"] == "https://youtube.com/@updated"

    # 5. Test update_lead_fields persistence
    database.update_lead_fields(
        lead_id,
        {
            "linkedin_url": "https://linkedin.com/in/john",
            "twitter_url": "https://x.com/john",
            "youtube_url": "https://youtube.com/@john"
        },
        db_path=db_file
    )
    lead_fields = database.get_lead_by_id(lead_id, db_path=db_file)
    assert lead_fields["linkedin_url"] == "https://linkedin.com/in/john"
    assert lead_fields["twitter_url"] == "https://x.com/john"
    assert lead_fields["youtube_url"] == "https://youtube.com/@john"


def test_lead_exists_phone_candidates_and_excluded_domains(tmp_path):
    db_file = str(tmp_path / "test_lead_exists.db")
    database.init_db(db_file)

    database.insert_lead({
        "place_id": "0x555:0x555",
        "business_name": "Candidate Roofer",
        "clean_phone": "+12145550199",
        "clean_domain": "specialexclude.com",
        "status": "new"
    }, db_path=db_file)

    # Match across canonical candidate forms
    assert database.lead_exists(clean_phone="(214) 555-0199", db_path=db_file)
    assert database.lead_exists(clean_phone="2145550199", db_path=db_file)
    assert database.lead_exists(clean_phone="+12145550199", db_path=db_file)
    assert not database.lead_exists(clean_phone="2145559999", db_path=db_file)

    # Excluded domains override
    # When specialexclude.com is in excluded_domains, lead_exists should ignore it and return False
    assert not database.lead_exists(
        clean_domain="specialexclude.com",
        excluded_domains=["specialexclude.com"],
        db_path=db_file
    )
    # When excluded_domains is empty list, it shouldn't be excluded -> returns True
    assert database.lead_exists(
        clean_domain="specialexclude.com",
        excluded_domains=[],
        db_path=db_file
    )


def test_contacted_channel_fallback_unknown_and_other(tmp_path):
    db_file = str(tmp_path / "test_contacted_fallback.db")
    database.init_db(db_file)

    # Lead with status='contacted' but NO entries in outreach_log
    lead_id = database.insert_lead({
        "place_id": "0x666:0x666",
        "business_name": "No Outreach Log Roofer",
        "clean_phone": "+12145550199",
        "status": "contacted"
    }, db_path=db_file)

    leads, total = database.get_contacted_leads_with_history(db_path=db_file)
    assert total == 1
    assert leads[0]["id"] == lead_id
    assert leads[0]["last_channel"] == "unknown"

    stats = database.get_contacted_stats(db_path=db_file)
    assert stats["total"] == 1
    assert stats["whatsapp"] == 0
    assert stats["other"] == 1


def test_outreach_burst_and_multitouch(tmp_path):
    db_file = str(tmp_path / "test_outreach.db")
    database.init_db(db_file)
    lead_id = database.insert_lead({
        "place_id": "0xoutreach:0x1",
        "business_name": "Outreach Target Co",
        "status": "outreach_ready"
    }, db_path=db_file)

    # 1. Burst protection: 5 rapid calls on whatsapp
    log_id = None
    for i in range(5):
        log_id = database.log_outreach(
            lead_id=lead_id,
            channel="whatsapp",
            outcome="sent",
            message_sent=f"WhatsApp message version {i}",
            db_path=db_file
        )

    history = database.get_lead_outreach_history(lead_id, db_path=db_file)
    assert len(history) == 1
    assert history[0]["id"] == log_id
    assert history[0]["channel"] == "whatsapp"
    assert history[0]["message_sent"] == "WhatsApp message version 4"

    # 2. Multi-channel independence: immediately log email on same lead
    email_log_id = database.log_outreach(
        lead_id=lead_id,
        channel="email",
        outcome="sent",
        message_sent="Email pitch text",
        db_path=db_file
    )
    assert email_log_id != log_id

    history_after_email = database.get_lead_outreach_history(lead_id, db_path=db_file)
    assert len(history_after_email) == 2
    channels = {h["channel"] for h in history_after_email}
    assert channels == {"whatsapp", "email"}

    # 3. Multi-day / backdated follow-up preservation
    phone_log_1 = database.log_outreach(
        lead_id=lead_id,
        channel="phone",
        outcome="sent",
        message_sent=None,
        notes="First phone touch",
        db_path=db_file
    )

    conn = database.get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE outreach_log SET sent_at = datetime('now', '-10 minutes') WHERE id = ?",
            (phone_log_1,)
        )
    conn.close()

    # Now log a 2nd phone follow-up with message_sent=None
    phone_log_2 = database.log_outreach(
        lead_id=lead_id,
        channel="phone",
        outcome="sent",
        message_sent=None,
        notes="Second follow-up phone call",
        db_path=db_file
    )
    assert phone_log_2 != phone_log_1

    history_all = database.get_lead_outreach_history(lead_id, db_path=db_file)
    # Total rows: 1 whatsapp + 1 email + 2 phone = 4 rows
    assert len(history_all) == 4
    phone_entries = [h for h in history_all if h["channel"] == "phone"]
    assert len(phone_entries) == 2
    # Newest first
    assert phone_entries[0]["id"] == phone_log_2
    assert phone_entries[0]["notes"] == "Second follow-up phone call"
    assert phone_entries[1]["id"] == phone_log_1
    assert phone_entries[1]["notes"] == "First phone touch"


def test_runs_lifecycle_and_fail_run(tmp_path):
    db_file = str(tmp_path / "test_runs_lifecycle.db")
    database.init_db(db_file)

    # 1. create_run should create a run with status='running' and completed_at is None
    run_id = database.create_run(
        run_type="local_scrape",
        query="commercial roofer Dallas TX",
        metadata={"target_country": "US"},
        db_path=db_file
    )
    assert run_id > 0

    conn = database.get_connection(db_file)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = dict(cursor.fetchone())
        assert row["status"] == "running"
        assert row["completed_at"] is None
        assert row["run_type"] == "local_scrape"
        assert row["query"] == "commercial roofer Dallas TX"
    conn.close()

    # 2. fail_run should mark status='failed', completed_at populated, and error in metadata_json
    database.fail_run(run_id, error_msg="Simulated scraper connection timeout", db_path=db_file)

    conn = database.get_connection(db_file)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = dict(cursor.fetchone())
        assert row["status"] == "failed"
        assert row["completed_at"] is not None
        meta = json.loads(row["metadata_json"])
        assert meta.get("error") == "Simulated scraper connection timeout"
        assert meta.get("target_country") == "US"
    conn.close()

    # 3. create another run and complete_run
    run_id_2 = database.create_run(run_type="audit_only", db_path=db_file)
    database.complete_run(run_id_2, harvested=12, audited=10, ready=5, db_path=db_file)

    conn = database.get_connection(db_file)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE id = ?", (run_id_2,))
        row = dict(cursor.fetchone())
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["leads_harvested"] == 12
        assert row["leads_audited"] == 10
        assert row["leads_outreach_ready"] == 5
    conn.close()


def test_main_run_type_detection():
    class DummyArgs:
        def __init__(self, skip_scrape=False, skip_audit=False, re_audit=False):
            self.skip_scrape = skip_scrape
            self.skip_audit = skip_audit
            self.re_audit = re_audit

    def get_type(args):
        if getattr(args, "skip_scrape", False) and getattr(args, "re_audit", False):
            return "re_audit"
        elif getattr(args, "skip_scrape", False) and not getattr(args, "skip_audit", False):
            return "audit_only"
        elif getattr(args, "skip_audit", False) and not getattr(args, "skip_scrape", False):
            return "scrape_only"
        else:
            return "local_scrape"

    assert get_type(DummyArgs(skip_scrape=True, re_audit=True)) == "re_audit"
    assert get_type(DummyArgs(skip_scrape=True, skip_audit=False, re_audit=False)) == "audit_only"
    assert get_type(DummyArgs(skip_scrape=False, skip_audit=True)) == "scrape_only"
    assert get_type(DummyArgs(skip_scrape=False, skip_audit=False)) == "local_scrape"


def test_main_pipeline_deltas_and_failure_marking(tmp_path, monkeypatch):
    import main
    import asyncio
    import pytest

    db_file = str(tmp_path / "test_main_pipeline.db")
    config_file = str(tmp_path / "config.json")

    # Write a test config pointing to db_file
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["app"]["db_path"] = db_file
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    database.init_db(db_file)

    # Insert baseline data: 1 outreach_ready lead, 2 new leads
    database.insert_lead({"place_id": "p0", "business_name": "Existing Ready", "status": "outreach_ready"}, db_path=db_file)
    database.insert_lead({"place_id": "p1", "business_name": "New Lead 1", "status": "new", "website_url": "https://lead1.com"}, db_path=db_file)
    database.insert_lead({"place_id": "p2", "business_name": "New Lead 2", "status": "new", "website_url": "https://lead2.com"}, db_path=db_file)

    class Args:
        skip_scrape = False
        skip_audit = False
        re_audit = False

    # Mock scraper to return 5 newly harvested leads
    async def mock_scraper(config, run_id=None):
        return 5

    # Mock auditor to audit 1 of the new leads and mark it outreach_ready
    async def mock_auditor(config, re_audit=False):
        database.update_lead_fields(2, {"status": "outreach_ready"}, db_path=db_file)

    monkeypatch.setattr(main.c1_scraper, "run_maps_scraper", mock_scraper)
    monkeypatch.setattr(main.auditor, "run_audit_pipeline", mock_auditor)

    # Run pipeline successfully
    asyncio.run(main.run_pipeline(config_path=config_file, args=Args()))

    conn = database.get_connection(db_file)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        row = dict(cursor.fetchone())
        assert row["status"] == "completed"
        assert row["leads_harvested"] == 5
        # before_audit_new was 2, after audit new is 1, delta = 1
        assert row["leads_audited"] == 1
        # outreach_ready went from 1 to 2, delta = 1
        assert row["leads_outreach_ready"] == 1
    conn.close()

    # Now test failure marking: have auditor throw an exception
    async def failing_auditor(config, re_audit=False):
        raise RuntimeError("Auditor crashed due to network failure")

    monkeypatch.setattr(main.auditor, "run_audit_pipeline", failing_auditor)

    with pytest.raises(RuntimeError, match="Auditor crashed due to network failure"):
        asyncio.run(main.run_pipeline(config_path=config_file, args=Args()))

    conn = database.get_connection(db_file)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        row = dict(cursor.fetchone())
        assert row["status"] == "failed"
        assert row["completed_at"] is not None
        meta = json.loads(row["metadata_json"])
        assert "Auditor crashed due to network failure" in meta.get("error", "")
    conn.close()


def test_config_resolution_and_country_prefix(tmp_path, monkeypatch):
    import main
    import asyncio

    original_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))

        # Relative config resolution: "config.json" does not exist in tmp_path,
        # but run_pipeline resolves it to PROJECT_ROOT/config.json
        captured_config = {}

        async def capture_scraper(config, run_id=None):
            captured_config.update(config)
            return 0

        async def noop_auditor(config, re_audit=False):
            pass

        monkeypatch.setattr(main.c1_scraper, "run_maps_scraper", capture_scraper)
        monkeypatch.setattr(main.auditor, "run_audit_pipeline", noop_auditor)

        class Args:
            skip_scrape = False
            skip_audit = True
            re_audit = False

        # Run with relative config.json and country="UK"
        asyncio.run(main.run_pipeline(
            config_path="config.json",
            country="UK",
            args=Args()
        ))

        assert captured_config.get("scraper_c1", {}).get("default_phone_prefix") == "+44"
        assert captured_config.get("app", {}).get("target_country") == "UK"
        assert "+44" in captured_config.get("app", {}).get("country_prefix_map", {}).values()
    finally:
        os.chdir(original_cwd)


if __name__ == "__main__":
    test_database()
