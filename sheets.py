thisimport json
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
    """Maps a lead dictionary to an ordered row matching SHEET_HEADERS."""
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


def route_today_leads(spreadsheet: gspread.Spreadsheet, sheets_cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    Distributes intake leads from 'Today' to their bucket sheets,
    updates Archive master ledger, and flushes Today data rows.
    """
    tabs = sheets_cfg.get("tabs", {})
    today_tab = tabs.get("today", "Today")
    outreach_tab = tabs.get("outreach_ready", "Outreach Ready")
    missing_tab = tabs.get("missing_info", "Missing Info")
    disqualified_tab = tabs.get("disqualified", "Disqualified")
    archive_tab = tabs.get("archive", "Archive")

    today_sheet = ensure_worksheet(spreadsheet, today_tab)
    outreach_sheet = ensure_worksheet(spreadsheet, outreach_tab)
    missing_sheet = ensure_worksheet(spreadsheet, missing_tab)
    disqualified_sheet = ensure_worksheet(spreadsheet, disqualified_tab)
    archive_sheet = ensure_worksheet(spreadsheet, archive_tab)

    all_today_rows = today_sheet.get_all_values()
    if len(all_today_rows) <= 1:
        logging.info(f"'{today_tab}' has no data rows to route.")
        return {"outreach_ready": 0, "missing_info": 0, "disqualified": 0, "archive_new": 0, "archive_updated": 0}

    data_rows = all_today_rows[1:]
    # Pad every row to full 29 columns
    padded_rows = [r + [""] * (len(SHEET_HEADERS) - len(r)) if len(r) < len(SHEET_HEADERS) else r for r in data_rows]

    outreach_rows, missing_rows, disqualified_rows = [], [], []
    for r in padded_rows:
        status = r[26].strip()
        if status == "Outreach Ready":
            outreach_rows.append(r)
        elif status == "Missing Info":
            missing_rows.append(r)
        elif status == "Disqualified":
            disqualified_rows.append(r)
        else:
            logging.warning(f"Unrecognized status '{status}' for lead {r[0]}.")

    # Distribute to bucket sheets (deduplicate by UUID)
    for rows, sheet, name in [
        (outreach_rows, outreach_sheet, outreach_tab),
        (missing_rows, missing_sheet, missing_tab),
        (disqualified_rows, disqualified_sheet, disqualified_tab),
    ]:
        if rows:
            existing_uuids = set(sheet.col_values(1)[1:])
            to_append = [r for r in rows if r[0].strip() not in existing_uuids]
            if to_append:
                sheet.append_rows(to_append, value_input_option="RAW")
                logging.info(f"Appended {len(to_append)} leads to '{name}'.")

    # Archive: append new, update status of existing (by UUID)
    archive_rows = archive_sheet.get_all_values()
    existing_archive_map = {r[0].strip(): idx for idx, r in enumerate(archive_rows[1:], start=2) if r and r[0]}

    archive_appends = []
    archive_updates = []
    for r in padded_rows:
        uuid_val = r[0].strip()
        if not uuid_val:
            continue
        if uuid_val in existing_archive_map:
            row_num = existing_archive_map[uuid_val]
            archive_updates.append({"range": f"AA{row_num}:AB{row_num}", "values": [[r[26], r[27]]]})
        else:
            archive_appends.append(r)
            existing_archive_map[uuid_val] = len(archive_rows) + len(archive_appends)

    if archive_appends:
        archive_sheet.append_rows(archive_appends, value_input_option="RAW")
        logging.info(f"Appended {len(archive_appends)} new leads to '{archive_tab}'.")
    if archive_updates:
        archive_sheet.batch_update(archive_updates, value_input_option="RAW")
        logging.info(f"Updated status for {len(archive_updates)} leads in '{archive_tab}'.")

    # Flush Today intake buffer (keep row 1 headers)
    today_sheet.batch_clear(["A2:AC"])
    logging.info(f"Flushed {len(data_rows)} rows from '{today_tab}'. Headers preserved.")

    return {
        "outreach_ready": len(outreach_rows),
        "missing_info": len(missing_rows),
        "disqualified": len(disqualified_rows),
        "archive_new": len(archive_appends),
        "archive_updated": len(archive_updates)
    }


def sync_to_sheets(config) -> int:
    """
    Pushes pending audited leads from SQLite into 'Today',
    distributes to target tabs, updates Archive, and flushes Today.
    """
    if isinstance(config, str):
        with open(config, "r", encoding="utf-8") as f:
            config = json.load(f)

    db_path = config["app"]["db_path"]
    sheets_cfg = config["sheets"]

    logging.info(f"Authenticating with Google Sheets via {sheets_cfg['credentials_file']}...")
    try:
        gc = gspread.service_account(filename=sheets_cfg["credentials_file"])
        spreadsheet = gc.open_by_key(sheets_cfg["spreadsheet_id"])
    except Exception as e:
        logging.error(f"Failed to connect to Google Spreadsheet: {e}")
        return 0

    # Ensure all 5 worksheets exist with headers
    for tab_name in sheets_cfg.get("tabs", {}).values():
        ensure_worksheet(spreadsheet, tab_name)

    # Stage unsynced audited leads into Today buffer
    unsynced_leads = get_unsynced_leads(db_path)
    today_tab = sheets_cfg["tabs"].get("today", "Today")
    today_sheet = spreadsheet.worksheet(today_tab)

    if unsynced_leads:
        rows_to_append = [format_lead_row(lead) for lead in unsynced_leads]
        lead_uuids = [lead["lead_uuid"] for lead in unsynced_leads]
        logging.info(f"Appending {len(rows_to_append)} rows to '{today_tab}' buffer...")
        today_sheet.append_rows(rows_to_append, value_input_option="RAW")
        mark_leads_as_synced(lead_uuids, db_path)
        logging.info(f"Staged {len(rows_to_append)} leads into '{today_tab}'.")

    # Route from Today to destination bucket sheets
    stats = route_today_leads(spreadsheet, sheets_cfg)
    total_routed = stats["outreach_ready"] + stats["missing_info"] + stats["disqualified"]
    logging.info(f"Distribution complete: {stats}")
    return total_routed


if __name__ == "__main__":
    sync_to_sheets("config.json")
