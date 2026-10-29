import json
import sqlite3
import uuid
from typing import Optional, Dict, Any, List


def get_default_config() -> Dict[str, Any]:
    """Loads default configuration from config.json if present."""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Creates a database connection with dict-like row access and concurrency timeout."""
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initializes tables, WAL mode, and indexes for the lead extraction pipeline."""
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")
    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_uuid TEXT UNIQUE NOT NULL,
                    business_name TEXT NOT NULL,
                    niche TEXT,
                    city TEXT,
                    country TEXT,
                    maps_place_id TEXT,
                    maps_url TEXT,
                    rating REAL DEFAULT 0.0,
                    review_count INTEGER DEFAULT 0,
                    clean_domain TEXT,
                    website_url TEXT,
                    phone TEXT,
                    clean_phone TEXT,
                    lead_origin_pipeline TEXT,
                    search_query_raw TEXT,

                    -- Technical Audit Fields
                    load_time_sec REAL,
                    ttfb_ms INTEGER,
                    page_weight_mb REAL,
                    dom_nodes INTEGER,
                    mobile_broken INTEGER DEFAULT 0,
                    cms_detected TEXT,
                    failed_major_test INTEGER DEFAULT 0,
                    primary_flaw_label TEXT,

                    -- Decision Maker & Contact Fields
                    dm_name TEXT,
                    dm_title TEXT,
                    dm_email TEXT,
                    dm_linkedin TEXT,
                    generic_email TEXT,

                    -- Status Tracking
                    primary_status TEXT NOT NULL DEFAULT 'Today',
                    sub_status TEXT NOT NULL DEFAULT 'Pending Evaluation',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Speed indexes for O(1) deduplication and queue queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clean_domain ON leads(clean_domain)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_maps_place_id ON leads(maps_place_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clean_phone ON leads(clean_phone)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON leads(primary_status, sub_status)")
    finally:
        conn.close()


def is_duplicate(
    clean_domain: Optional[str] = None,
    maps_place_id: Optional[str] = None,
    clean_phone: Optional[str] = None,
    db_path: Optional[str] = None,
    excluded_domains: Optional[List[str]] = None
) -> bool:
    """
    Checks if an entity already exists in the database.
    Evaluates Domain -> Maps Place ID -> Phone.
    Reads excluded_domains from config.json dynamically if not provided.
    """
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")

    if excluded_domains is None:
        excluded_domains = get_default_config().get("app", {}).get("excluded_domains", [])

    ignored_domains = set(d.lower() for d in excluded_domains) if excluded_domains else set()
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()

        if clean_domain and clean_domain.lower() not in ignored_domains:
            cursor.execute("SELECT 1 FROM leads WHERE clean_domain = ? LIMIT 1", (clean_domain,))
            if cursor.fetchone():
                return True

        if maps_place_id:
            cursor.execute("SELECT 1 FROM leads WHERE maps_place_id = ? LIMIT 1", (maps_place_id,))
            if cursor.fetchone():
                return True

        if clean_phone:
            cursor.execute("SELECT 1 FROM leads WHERE clean_phone = ? LIMIT 1", (clean_phone,))
            if cursor.fetchone():
                return True

        return False
    finally:
        conn.close()


def insert_lead(lead_data: Dict[str, Any], db_path: Optional[str] = None) -> int:
    """Inserts a new raw lead into the staging state."""
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")

    if "lead_uuid" not in lead_data or not lead_data["lead_uuid"]:
        lead_data["lead_uuid"] = uuid.uuid4().hex

    fields = [
        "lead_uuid", "business_name", "niche", "city", "country",
        "maps_place_id", "maps_url", "rating", "review_count",
        "clean_domain", "website_url", "phone", "clean_phone",
        "lead_origin_pipeline", "search_query_raw",
        "primary_status", "sub_status"
    ]

    keys_to_insert = [k for k in fields if k in lead_data]
    placeholders = ", ".join(["?"] * len(keys_to_insert))
    columns = ", ".join(keys_to_insert)
    values = [lead_data[k] for k in keys_to_insert]

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
    """Fetches leads that have a website but have not been audited yet."""
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")

    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM leads
            WHERE website_url IS NOT NULL
              AND website_url != ''
              AND load_time_sec IS NULL
              AND primary_status = 'Today'
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_lead_audit(
    lead_id: int,
    audit_data: Dict[str, Any],
    primary_status: str,
    sub_status: str,
    db_path: Optional[str] = None
) -> None:
    """Updates technical audit results, DM contact information, and state."""
    if not db_path:
        db_path = get_default_config().get("app", {}).get("db_path", "lead_pipeline.db")

    allowed_fields = [
        "load_time_sec", "ttfb_ms", "page_weight_mb", "dom_nodes",
        "mobile_broken", "cms_detected", "failed_major_test",
        "primary_flaw_label", "dm_name", "dm_title", "dm_email",
        "dm_linkedin", "generic_email"
    ]

    set_clauses = ["primary_status = ?", "sub_status = ?", "updated_at = CURRENT_TIMESTAMP"]
    values = [primary_status, sub_status]

    for field in allowed_fields:
        if field in audit_data:
            set_clauses.append(f"{field} = ?")
            values.append(audit_data[field])

    values.append(lead_id)

    conn = get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE leads SET {', '.join(set_clauses)} WHERE id = ?", values)
    finally:
        conn.close()
