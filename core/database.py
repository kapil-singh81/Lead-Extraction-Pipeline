import difflib
import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from typing import Optional, Dict, Any, List, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_CACHED_CONFIG: Optional[Dict[str, Any]] = None


def get_default_config() -> Dict[str, Any]:
    """Loads configuration from config.json on first call, then returns cached dict."""
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None:
        cfg_path = os.path.join(PROJECT_ROOT, "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                _CACHED_CONFIG = json.load(f)
        except Exception:
            _CACHED_CONFIG = {}
    return _CACHED_CONFIG


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Returns an SQLite connection in WAL mode with Foreign Keys enabled
    and row dictionary factory.
    """
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "data/lead_pipeline.db")
    resolved_db_path = os.path.join(PROJECT_ROOT, db_path) if not os.path.isabs(db_path) else db_path
    conn = sqlite3.connect(resolved_db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initializes the SQLite database with the redesigned 3-table schema:
    1. runs: Tracks execution batches (local scrape / kaggle imports)
    2. leads: Core businesses and audits (identity via unique place_id)
    3. outreach_log: Multi-touch outreach actions per lead
    Plus phone_prefixes for offline NANPA lookups.
    """
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()

            # Ensure legacy sync_log table is dropped
            cursor.execute("DROP TABLE IF EXISTS sync_log;")

            # 1. Runs Tracking Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_uuid TEXT UNIQUE NOT NULL,
                    run_type TEXT NOT NULL DEFAULT 'local_scrape',
                    query TEXT,
                    leads_harvested INTEGER DEFAULT 0,
                    leads_audited INTEGER DEFAULT 0,
                    leads_outreach_ready INTEGER DEFAULT 0,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    status TEXT DEFAULT 'completed',
                    metadata_json TEXT
                );
            """)

            # Recreate fresh leads table if legacy schema exists without place_id
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='leads';")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(leads);")
                existing_cols = {row["name"] for row in cursor.fetchall()}
                if "place_id" not in existing_cols:
                    logging.warning("Legacy leads schema detected without place_id. Recreating fresh redesign schema.")
                    cursor.execute("DROP TABLE leads;")

            # 2. Leads Master Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
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

            # 3. Outreach Log Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outreach_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    message_sent TEXT,
                    outcome TEXT DEFAULT 'sent',
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    next_followup_at TIMESTAMP,
                    notes TEXT
                );
            """)

            # 4. Offline Phone Prefixes Table (NANPA)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS phone_prefixes (
                    npa TEXT,
                    nxx TEXT,
                    line_type TEXT,
                    PRIMARY KEY (npa, nxx)
                );
            """)

            # Speed indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_place_id ON leads(place_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_status_priority ON leads(status, priority_score DESC);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_clean_domain ON leads(clean_domain);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_clean_phone ON leads(clean_phone);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_deferred ON leads(deferred_until);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_updated_at ON leads(updated_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_outreach_lead_id ON outreach_log(lead_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_outreach_sent_at ON outreach_log(sent_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone_prefix_lookup ON phone_prefixes(npa, nxx);")

            # Migration: ensure instagram_url and whatsapp_url exist in existing leads table
            try:
                cursor.execute("PRAGMA table_info(leads);")
                cols = {r["name"] for r in cursor.fetchall()}
                if "instagram_url" not in cols:
                    cursor.execute("ALTER TABLE leads ADD COLUMN instagram_url TEXT;")
                if "whatsapp_url" not in cols:
                    cursor.execute("ALTER TABLE leads ADD COLUMN whatsapp_url TEXT;")
            except Exception:
                pass
    finally:
        conn.close()


def normalize_business_name(name: str) -> str:
    """Removes common corporate legal suffixes and punctuation for fuzzy de-aliasing."""
    if not name:
        return ""
    norm = name.lower()
    norm = re.sub(r"[^\w\s]", "", norm)
    suffixes = [
        r"\bllc\b", r"\binc\b", r"\bcorp\b", r"\bco\b",
        r"\bltd\b", r"\bservices\b", r"\bgroup\b", r"\bcompany\b"
    ]
    for s in suffixes:
        norm = re.sub(s, "", norm)
    return " ".join(norm.split())


def lead_exists(
    place_id: Optional[str] = None,
    clean_domain: Optional[str] = None,
    clean_phone: Optional[str] = None,
    business_name: Optional[str] = None,
    city: Optional[str] = None,
    db_path: Optional[str] = None,
    fuzzy_threshold: float = 0.88
) -> bool:
    """
    Checks if a lead already exists in SQLite using place_id, clean_domain, clean_phone,
    or fuzzy business name similarity within the same city.
    """
    if not place_id and not clean_domain and not clean_phone and not business_name:
        return False

    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()

        if place_id:
            cursor.execute("SELECT 1 FROM leads WHERE place_id = ? LIMIT 1", (place_id,))
            if cursor.fetchone():
                return True

        if clean_domain:
            cfg = get_default_config()
            excluded = set(cfg.get("app", {}).get("excluded_domains", []))
            if clean_domain.lower() not in excluded:
                cursor.execute("SELECT 1 FROM leads WHERE clean_domain = ? LIMIT 1", (clean_domain,))
                if cursor.fetchone():
                    return True

        if clean_phone and len(re.sub(r"\D", "", clean_phone)) >= 10:
            norm_phone = re.sub(r"\D", "", clean_phone)[-10:]
            cursor.execute("SELECT 1 FROM leads WHERE clean_phone LIKE ? LIMIT 1", (f"%{norm_phone}%",))
            if cursor.fetchone():
                return True

        if business_name and city:
            norm_candidate = normalize_business_name(business_name)
            if len(norm_candidate) >= 4:
                cursor.execute("SELECT business_name FROM leads WHERE city = ? COLLATE NOCASE", (city,))
                existing_names = [row["business_name"] for row in cursor.fetchall() if row["business_name"]]
                for existing in existing_names:
                    norm_existing = normalize_business_name(existing)
                    if difflib.SequenceMatcher(None, norm_candidate, norm_existing).ratio() >= fuzzy_threshold:
                        return True

        return False
    finally:
        conn.close()


def insert_lead(lead_data: Dict[str, Any], db_path: Optional[str] = None) -> int:
    """Inserts a new raw lead into SQLite with status='new'."""
    fields = [
        "run_id", "place_id", "business_name", "niche", "city", "country",
        "website_url", "clean_domain", "phone", "clean_phone", "line_type",
        "facebook_url", "instagram_url", "whatsapp_url", "form_url", "dm_name", "dm_title", "dm_email",
        "dm_linkedin", "generic_email", "rating", "review_count",
        "review_snippet", "maps_url", "lead_origin", "search_query",
        "status", "status_reason", "priority_score", "audit_json", "manual_notes"
    ]

    keys = [k for k in fields if k in lead_data]
    columns = ", ".join(keys)
    placeholders = ", ".join(["?"] * len(keys))
    values = [lead_data[k] for k in keys]

    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO leads ({columns}) VALUES ({placeholders})",
                values
            )
            return cursor.lastrowid
    finally:
        conn.close()


def get_unaudited_leads(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetches leads that have not completed an audit yet."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM leads
            WHERE audit_json IS NULL
              AND status = 'new'
            ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_leads_for_reaudit(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetches all leads that have a website for re-auditing."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM leads
            WHERE website_url IS NOT NULL 
              AND TRIM(website_url) != ''
              AND LOWER(TRIM(website_url)) NOT IN ('none', 'n/a', 'null', 'undefined', 'unknown')
            ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_lead_audit(
    lead_id: int,
    audit_data: Dict[str, Any],
    status: str,
    status_reason: str,
    priority_score: float,
    db_path: Optional[str] = None
) -> None:
    """Updates technical audit results, classification status, and priority score."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            audit_json_str = json.dumps(audit_data)

            updates = [
                "status = ?",
                "status_reason = ?",
                "priority_score = ?",
                "audit_json = ?",
                "updated_at = CURRENT_TIMESTAMP"
            ]
            values = [status, status_reason, priority_score, audit_json_str]

            for field in ("dm_name", "dm_title", "dm_email", "dm_linkedin", "generic_email", "facebook_url", "instagram_url", "whatsapp_url", "form_url", "line_type"):
                if field in audit_data and audit_data[field] is not None:
                    updates.append(f"{field} = ?")
                    values.append(audit_data[field])

            values.append(lead_id)
            cursor.execute(
                f"UPDATE leads SET {', '.join(updates)} WHERE id = ?",
                values
            )
    finally:
        conn.close()


def get_daily_queue(daily_queue_size: int = 35, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns today's review progress and remaining queue items.
    Prevents endless refills by calculating done_today.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()

        # Count leads processed today by human review (outreach logged or manually triaged in cockpit)
        cursor.execute("""
            SELECT (
                (SELECT COUNT(DISTINCT lead_id) FROM outreach_log WHERE date(sent_at, 'localtime') = date('now', 'localtime'))
                +
                (SELECT COUNT(*) FROM leads WHERE date(updated_at, 'localtime') = date('now', 'localtime')
                    AND (manual_notes LIKE '%[reviewed]%' OR status = 'deferred')
                    AND id NOT IN (SELECT lead_id FROM outreach_log WHERE date(sent_at, 'localtime') = date('now', 'localtime')))
            ) AS done_today;
        """)
        done_today = cursor.fetchone()["done_today"] or 0
        remaining_quota = max(0, daily_queue_size - done_today)

        leads = []
        if remaining_quota > 0:
            cursor.execute("""
                SELECT * FROM leads
                WHERE status = 'outreach_ready'
                  AND (deferred_until IS NULL OR deferred_until <= datetime('now', 'localtime'))
                ORDER BY priority_score DESC
                LIMIT ?
            """, (remaining_quota,))
            leads = [dict(row) for row in cursor.fetchall()]

        return {
            "daily_queue_size": daily_queue_size,
            "done_today": done_today,
            "remaining_quota": remaining_quota,
            "leads": leads
        }
    finally:
        conn.close()


def update_lead_status(
    lead_id: int,
    new_status: str,
    reason: Optional[str] = None,
    notes: Optional[str] = None,
    deferred_until: Optional[str] = None,
    db_path: Optional[str] = None
) -> None:
    """Updates a lead's qualification/review status and optional notes/deferral."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
            values = [new_status]

            if reason is not None:
                updates.append("status_reason = ?")
                values.append(reason)
            if notes is not None:
                updates.append("manual_notes = ?")
                values.append(notes)
            if deferred_until is not None:
                updates.append("deferred_until = ?")
                values.append(deferred_until)

            values.append(lead_id)
            cursor.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", values)
    finally:
        conn.close()


def update_lead_fields(
    lead_id: int,
    fields: Dict[str, Any],
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Safely updates arbitrary editable fields on a lead in the database.
    Allows correcting business name, phone, clean_phone, line_type, website_url,
    clean_domain, dm_name, dm_title, dm_email, dm_linkedin, generic_email,
    city, niche, status, status_reason, manual_notes, rating, review_count, etc.
    """
    ALLOWED_FIELDS = {
        "business_name", "niche", "city", "country", "website_url", "clean_domain",
        "phone", "clean_phone", "line_type", "facebook_url", "instagram_url",
        "whatsapp_url", "form_url", "dm_name", "dm_title", "dm_email",
        "dm_linkedin", "generic_email", "rating", "review_count", "review_snippet",
        "maps_url", "status", "status_reason", "manual_notes", "deferred_until"
    }

    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            updates = []
            values = []

            for k, v in fields.items():
                if k in ALLOWED_FIELDS:
                    updates.append(f"{k} = ?")
                    # Clean up clean_domain and ensure scheme if website_url is updated
                    if k == "website_url" and v:
                        v_str = str(v).strip()
                        if v_str.lower() != "none" and not (v_str.startswith("http://") or v_str.startswith("https://")):
                            v = f"https://{v_str}"
                        clean_dom = re.sub(r"^https?://(www\.)?", "", str(v).strip(), flags=re.IGNORECASE).rstrip("/").lower()
                        updates.append("clean_domain = ?")
                        values.append(clean_dom)
                    # Clean up clean_phone if phone is updated
                    if k == "phone" and v:
                        digits = re.sub(r"\D", "", str(v))
                        if len(digits) == 10:
                            clean_p = f"+1{digits}"
                        elif len(digits) == 11 and digits.startswith("1"):
                            clean_p = f"+{digits}"
                        else:
                            clean_p = str(v).strip()
                        updates.append("clean_phone = ?")
                        values.append(clean_p)
                    values.append(v)

            if not updates:
                return get_lead_by_id(lead_id, db_path=db_path)

            updates.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE leads SET {', '.join(updates)} WHERE id = ?"
            values.append(lead_id)
            cursor.execute(sql, values)

        return get_lead_by_id(lead_id, db_path=db_path)
    finally:
        conn.close()


def log_outreach(
    lead_id: int,
    channel: str,
    outcome: str = "sent",
    message_sent: Optional[str] = None,
    notes: Optional[str] = None,
    next_followup_at: Optional[str] = None,
    db_path: Optional[str] = None
) -> int:
    """Logs an outreach interaction and marks lead as 'contacted', preventing duplicate entries."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            # Deduplication: check if an entry with same lead_id, channel, and message exists recently
            cursor.execute("""
                SELECT id FROM outreach_log
                WHERE lead_id = ? AND LOWER(channel) = LOWER(?)
                  AND (
                    (message_sent = ? OR (message_sent IS NULL AND ? IS NULL))
                    OR sent_at >= datetime('now', '-2 minutes')
                  )
                ORDER BY id DESC LIMIT 1
            """, (lead_id, channel, message_sent, message_sent))
            existing = cursor.fetchone()
            if existing:
                log_id = existing["id"]
                cursor.execute("""
                    UPDATE outreach_log
                    SET outcome = ?,
                        message_sent = COALESCE(?, message_sent),
                        notes = COALESCE(?, notes),
                        sent_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (outcome, message_sent, notes, log_id))
            else:
                cursor.execute("""
                    INSERT INTO outreach_log (lead_id, channel, message_sent, outcome, next_followup_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (lead_id, channel, message_sent, outcome, next_followup_at, notes))
                log_id = cursor.lastrowid

            cursor.execute("""
                UPDATE leads
                SET status = 'contacted',
                    status_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (f"Contacted via {channel}", lead_id))
            return log_id
    finally:
        conn.close()


def delete_outreach_log(log_id: int, db_path: Optional[str] = None) -> bool:
    """Deletes an outreach log entry by ID."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM outreach_log WHERE id = ?", (log_id,))
            return cursor.rowcount > 0
    finally:
        conn.close()


def get_pipeline_stats(db_path: Optional[str] = None) -> Dict[str, int]:
    """Returns aggregated lead counts by status."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as count FROM leads GROUP BY status")
        stats = {row["status"]: row["count"] for row in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) as total FROM leads")
        stats["total"] = cursor.fetchone()["total"]
        return stats
    finally:
        conn.close()


def create_run(run_type: str = "local_scrape", query: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, db_path: Optional[str] = None) -> int:
    """Initializes a new tracking run entry in SQLite."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            run_uuid = uuid.uuid4().hex
            meta_str = json.dumps(metadata or {})
            cursor.execute("""
                INSERT INTO runs (run_uuid, run_type, query, metadata_json)
                VALUES (?, ?, ?, ?)
            """, (run_uuid, run_type, query, meta_str))
            return cursor.lastrowid
    finally:
        conn.close()


def complete_run(run_id: int, harvested: int, audited: int, ready: int, db_path: Optional[str] = None) -> None:
    """Marks a tracking run as completed with aggregate counts."""
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE runs
                SET leads_harvested = ?,
                    leads_audited = ?,
                    leads_outreach_ready = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    status = 'completed'
                WHERE id = ?
            """, (harvested, audited, ready, run_id))
    finally:
        conn.close()


def get_lead_by_id(lead_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a single lead by its primary key ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_lead_outreach_history(lead_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves all outreach interactions for a specific lead, deduplicating identical records."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, lead_id, channel, message_sent, outcome, sent_at, next_followup_at, notes
            FROM outreach_log
            WHERE lead_id = ?
            GROUP BY channel, outcome, COALESCE(message_sent, ''), COALESCE(notes, ''), substr(sent_at, 1, 16)
            ORDER BY sent_at DESC, id DESC
        """, (lead_id,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_leads_master(
    status: Optional[str] = None,
    search: Optional[str] = None,
    channel: Optional[str] = None,
    flaw_type: Optional[str] = None,
    min_rating: Optional[float] = None,
    limit: int = 50,
    offset: int = 0,
    db_path: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Returns filtered leads and total count with full omni-search capabilities:
    - Search: matches id, business_name, city, domain, phone, niche, dm_name, dm_email, flaw keywords, status_reason, audit_json
    - Filters: channel, flaw_type, min_rating, status
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        where_clauses = []
        params: List[Any] = []

        if status and status.lower() != "all":
            where_clauses.append("status = ?")
            params.append(status.lower())

        if search and search.strip():
            raw_s = search.strip()
            term = f"%{raw_s}%"
            id_check = ""
            if raw_s.lstrip("#").isdigit():
                id_val = int(raw_s.lstrip("#"))
                id_check = "id = ? OR "
                params.append(id_val)

            where_clauses.append(f"""(
                {id_check}
                business_name LIKE ? OR 
                city LIKE ? OR 
                clean_domain LIKE ? OR 
                website_url LIKE ? OR 
                clean_phone LIKE ? OR 
                phone LIKE ? OR 
                niche LIKE ? OR 
                dm_name LIKE ? OR 
                dm_email LIKE ? OR 
                generic_email LIKE ? OR 
                status_reason LIKE ? OR 
                audit_json LIKE ?
            )""")
            params.extend([term] * 12)

        if channel and channel.lower() != "all":
            ch = channel.lower()
            if ch == "whatsapp":
                where_clauses.append("(whatsapp_url IS NOT NULL AND whatsapp_url != '')")
            elif ch == "phone":
                where_clauses.append("(clean_phone IS NOT NULL AND clean_phone != '')")
            elif ch == "email":
                where_clauses.append("(dm_email IS NOT NULL AND dm_email != '' OR generic_email IS NOT NULL AND generic_email != '')")
            elif ch == "form":
                where_clauses.append("(form_url IS NOT NULL AND form_url != '')")

        if flaw_type and flaw_type.lower() != "all":
            flaw_term = f"%{flaw_type.strip().lower()}%"
            where_clauses.append("(audit_json LIKE ? OR status_reason LIKE ?)")
            params.extend([flaw_term, flaw_term])

        if min_rating and min_rating > 0:
            where_clauses.append("rating >= ?")
            params.append(min_rating)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        cursor.execute(f"SELECT COUNT(*) as total FROM leads {where_sql}", params)
        total_count = cursor.fetchone()["total"]

        cursor.execute(f"""
            SELECT * FROM leads
            {where_sql}
            ORDER BY priority_score DESC, updated_at DESC
            LIMIT ? OFFSET ?
        """, (*params, limit, offset))
        leads = [dict(row) for row in cursor.fetchall()]

        return leads, total_count
    finally:
        conn.close()


def get_contacted_leads_with_history(
    search: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db_path: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Returns contacted leads enriched with their latest outreach interaction from outreach_log:
    - last_channel, contacted_at, message_sent, contact_notes, contact_outcome, next_followup_at
    Supports omni-search (name, phone, city, notes, message, id) and channel filtering.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        where_clauses = ["l.status = 'contacted'"]
        params: List[Any] = []

        if search and search.strip():
            raw_s = search.strip()
            term = f"%{raw_s}%"
            id_check = ""
            if raw_s.lstrip("#").isdigit():
                id_val = int(raw_s.lstrip("#"))
                id_check = "l.id = ? OR "
                params.append(id_val)

            where_clauses.append(f"""(
                {id_check}
                l.business_name LIKE ? OR 
                l.city LIKE ? OR 
                l.clean_domain LIKE ? OR 
                l.clean_phone LIKE ? OR 
                l.phone LIKE ? OR 
                l.dm_name LIKE ? OR 
                o.message_sent LIKE ? OR 
                o.notes LIKE ?
            )""")
            params.extend([term] * 8)

        if channel and channel.lower() != "all":
            where_clauses.append("LOWER(COALESCE(o.channel, '')) = ?")
            params.append(channel.lower())

        where_sql = f"WHERE {' AND '.join(where_clauses)}"

        query_count = f"""
            SELECT COUNT(*) as total
            FROM leads l
            LEFT JOIN (
                SELECT lead_id, channel, sent_at, message_sent, notes, outcome, next_followup_at,
                       ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY sent_at DESC, id DESC) as rn
                FROM outreach_log
            ) o ON l.id = o.lead_id AND o.rn = 1
            {where_sql}
        """
        cursor.execute(query_count, params)
        total_count = cursor.fetchone()["total"]

        query_leads = f"""
            SELECT 
                l.*,
                COALESCE(o.channel, 'whatsapp') as last_channel,
                COALESCE(o.sent_at, l.updated_at) as contacted_at,
                o.message_sent,
                o.notes as contact_notes,
                o.outcome as contact_outcome,
                o.next_followup_at
            FROM leads l
            LEFT JOIN (
                SELECT lead_id, channel, sent_at, message_sent, notes, outcome, next_followup_at,
                       ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY sent_at DESC, id DESC) as rn
                FROM outreach_log
            ) o ON l.id = o.lead_id AND o.rn = 1
            {where_sql}
            ORDER BY contacted_at DESC, l.updated_at DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query_leads, (*params, limit, offset))
        rows = [dict(r) for r in cursor.fetchall()]
        return rows, total_count
    finally:
        conn.close()


def get_contacted_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Calculates summary KPIs for the Contacted Leads page based on each contacted lead's latest outreach channel."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as total FROM leads WHERE status = 'contacted';")
        total_contacted = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT LOWER(COALESCE(o.channel, 'whatsapp')) as channel, COUNT(*) as count
            FROM leads l
            LEFT JOIN (
                SELECT lead_id, channel,
                       ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY sent_at DESC, id DESC) as rn
                FROM outreach_log
            ) o ON l.id = o.lead_id AND o.rn = 1
            WHERE l.status = 'contacted'
            GROUP BY LOWER(COALESCE(o.channel, 'whatsapp'));
        """)
        channel_counts = {row["channel"]: row["count"] for row in cursor.fetchall()}

        return {
            "total": total_contacted,
            "whatsapp": channel_counts.get("whatsapp", 0),
            "phone": channel_counts.get("phone", 0),
            "email": channel_counts.get("email", 0),
            "form": channel_counts.get("form", 0),
            "facebook": channel_counts.get("facebook", 0),
            "other": max(0, total_contacted - sum(channel_counts.values()))
        }
    finally:
        conn.close()

