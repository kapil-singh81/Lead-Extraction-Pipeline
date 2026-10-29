import json
import logging
from typing import Dict, Any, List
import gspread
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SHEET_HEADERS = [
    "Lead UUID", "Business Name", "Niche", "City", "Country",
    "Website URL", "Phone", "Clean Phone", "Rating", "Review Count",
    "Maps URL", "Lead Origin", "Search Query",
    "Load Time (s)", "TTFB (ms)", "Page Weight (MB)", "DOM Nodes",
    "Mobile Broken", "CMS Detected", "Failed Major Test", "Primary Flaw",
    "DM Name", "DM Title", "DM Email", "DM LinkedIn", "Generic Email",
    "Primary Status", "Sub Status", "Date Harvested"
]


def init_sync_table(db_path: str) -> None:
    """Ensures local sync tracking table exists."""
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                lead_uuid TEXT PRIMARY KEY,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_unsynced_leads(db_path: str) -> List[Dict[str, Any]]:
    """Retrieves all audited leads that haven't been exported to Google Sheets."""
    init_sync_table(db_path)
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM leads
            WHERE lead_uuid NOT IN (SELECT lead_uuid FROM sync_log)
              AND primary_status IN ('Outreach Ready', 'Missing Info', 'Disqualified')
            ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


def mark_leads_as_synced(lead_uuids: List[str], db_path: str) -> None:
    """Records synced lead UUIDs to prevent duplicate exports."""
    if not lead_uuids:
        return
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT OR IGNORE INTO sync_log (lead_uuid) VALUES (?)",
            [(u,) for u in lead_uuids]
        )


def format_lead_row(lead: Dict[str, Any]) -> List[Any]:
    """Maps a lead dictionary to an ordered 29-column row matching SHEET_HEADERS."""
    return [
        str(lead.get("lead_uuid") or ""),
        str(lead.get("business_name") or ""),
        str(lead.get("niche") or ""),
        str(lead.get("city") or ""),
        str(lead.get("country") or ""),
        str(lead.get("website_url") or ""),
        str(lead.get("phone") or ""),
        str(lead.get("clean_phone") or ""),
        lead.get("rating") if lead.get("rating") is not None else 0.0,
        lead.get("review_count") if lead.get("review_count") is not None else 0,
        str(lead.get("maps_url") or ""),
        str(lead.get("lead_origin_pipeline") or ""),
        str(lead.get("search_query_raw") or ""),
        lead.get("load_time_sec") if lead.get("load_time_sec") is not None else "",
        lead.get("ttfb_ms") if lead.get("ttfb_ms") is not None else "",
        lead.get("page_weight_mb") if lead.get("page_weight_mb") is not None else "",
        lead.get("dom_nodes") if lead.get("dom_nodes") is not None else "",
        "Yes" if lead.get("mobile_broken") else "No",
        str(lead.get("cms_detected") or ""),
        "Yes" if lead.get("failed_major_test") else "No",
        str(lead.get("primary_flaw_label") or ""),
        str(lead.get("dm_name") or ""),
        str(lead.get("dm_title") or ""),
        str(lead.get("dm_email") or ""),
        str(lead.get("dm_linkedin") or ""),
        str(lead.get("generic_email") or ""),
        str(lead.get("primary_status") or ""),
        str(lead.get("sub_status") or ""),
        str(lead.get("created_at") or "")
    ]


def ensure_worksheet(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """Retrieves or creates a worksheet and ensures standard 29-column headers exist in Row 1."""
    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        logging.info(f"Worksheet '{tab_name}' not found. Creating it...")
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(SHEET_HEADERS))

    if not sheet.row_values(1):
        logging.info(f"Writing headers to '{tab_name}' tab...")
        sheet.update(range_name="A1", values=[SHEET_HEADERS])

    return sheet


def sync_to_sheets(config) -> int:
    """
    Pulls unsynced audited leads from SQLite and batch appends them
    directly into the intake worksheet ('Today') in a single call.
    """
    if isinstance(config, str):
        with open(config, "r", encoding="utf-8") as f:
            config = json.load(f)

    db_path = config["app"]["db_path"]
    sheets_cfg = config["sheets"]
    credentials_file = sheets_cfg.get("credentials_file", "credentials.json")
    spreadsheet_id = sheets_cfg.get("spreadsheet_id")
    today_tab = sheets_cfg.get("tabs", {}).get("today", "Today")

    logging.info(f"Authenticating with Google Sheets via {credentials_file}...")
    try:
        gc = gspread.service_account(filename=credentials_file)
        spreadsheet = gc.open_by_key(spreadsheet_id)
    except Exception as e:
        logging.error(f"Failed to connect to Google Spreadsheet: {e}")
        return 0

    today_sheet = ensure_worksheet(spreadsheet, today_tab)
    unsynced_leads = get_unsynced_leads(db_path)

    if not unsynced_leads:
        logging.info("No unsynced leads pending export.")
        return 0

    rows_to_append = [format_lead_row(lead) for lead in unsynced_leads]
    lead_uuids = [lead["lead_uuid"] for lead in unsynced_leads]

    logging.info(f"Batch appending {len(rows_to_append)} rows to '{today_tab}'...")
    today_sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
    mark_leads_as_synced(lead_uuids, db_path)

    logging.info(f"Successfully synced {len(rows_to_append)} leads to '{today_tab}'.")
    return len(rows_to_append)


if __name__ == "__main__":
    sync_to_sheets("config.json")
