"""
inspect_db.py - Inspects lead pipeline database tables, counts, and outreach records.
"""

import os
import sys
import sqlite3
import argparse
from typing import Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import database


def inspect_database(db_path: Optional[str] = None) -> None:
    if db_path is None:
        cfg = database.get_default_config()
        db_path = cfg.get("app", {}).get("db_path", "data/lead_pipeline.db")

    if not os.path.isabs(db_path):
        resolved_path = os.path.normpath(os.path.join(PROJECT_ROOT, db_path))
    else:
        resolved_path = os.path.normpath(db_path)

    if not os.path.exists(resolved_path):
        print(f"Database file not found: {resolved_path}")
        return

    conn = sqlite3.connect(resolved_path)
    try:
        c = conn.cursor()

        print("=== CONTACTED LEADS IN DB ===")
        for r in c.execute("SELECT id, business_name, status, phone FROM leads WHERE status = 'contacted' ORDER BY id"):
            print(r)

        print("\n=== OUTREACH LOG ROWS ===")
        for r in c.execute("SELECT id, lead_id, sent_at, channel, outcome, notes FROM outreach_log ORDER BY id"):
            print(r)

        print("\n=== STATUS COUNTS IN LEADS ===")
        for r in c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status"):
            print(r)

        print("\n=== CHANNEL COUNTS IN OUTREACH LOG ===")
        for r in c.execute("SELECT channel, COUNT(*) FROM outreach_log GROUP BY channel"):
            print(r)

        print("\n=== DISTINCT LEADS IN OUTREACH LOG PER CHANNEL ===")
        for r in c.execute("SELECT channel, COUNT(DISTINCT lead_id) FROM outreach_log GROUP BY channel"):
            print(r)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect lead pipeline database tables and counts.")
    parser.add_argument("--db-path", dest="db_path", default=None, help="Path to SQLite database file.")
    args = parser.parse_args()
    inspect_database(db_path=args.db_path)
