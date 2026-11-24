"""
seed_nanpa.py - Downloads and seeds the official, free FCC/NANPA public NPA-NXX
telecom table into SQLite for 100% offline, zero-cost US line-type classification.
"""

import os
import sys
import io
import csv
import zipfile
import logging
import argparse
import urllib.request
from typing import Optional, List, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NANPA_URL = "https://www.nationalnanpa.com/nanp1/allutlzd.zip"


def classify_line_type(use_val: str) -> str:
    """
    Classifies phone line type strictly from the NANPA Use column value.
    Never inspects carrier names, company names, or city names.
    """
    u = (use_val or "").strip().upper()
    if any(k in u for k in ("WIRELESS", "MOBILE", "CELLULAR", "PCS", "CMRS")):
        return "Mobile"
    if "VOIP" in u:
        return "VoIP"
    if any(k in u for k in ("PAGING", "PAGER")):
        return "Paging"
    if any(k in u for k in ("WIRELINE", "LANDLINE", "LOCAL", "CLEC", "ILEC", "UA", "AS")):
        return "Landline"
    return "Other"


def parse_nanpa_stream(text_stream) -> List[Tuple[str, str, str]]:
    """
    Parses a NANPA text stream or iterable of lines into a list of (npa, nxx, line_type) tuples.
    Auto-detects delimiter (\\t, |, or ,) and handles headers or positional fallbacks.
    """
    if isinstance(text_stream, str):
        lines = [line for line in text_stream.splitlines() if line.strip()]
    elif isinstance(text_stream, (list, tuple)):
        lines = [
            (line.decode("latin-1") if isinstance(line, bytes) else line).strip()
            for line in text_stream
            if line
        ]
        lines = [l for l in lines if l]
    else:
        # File object or TextIOBase
        lines = []
        for line in text_stream:
            decoded = line.decode("latin-1") if isinstance(line, bytes) else line
            stripped = decoded.strip()
            if stripped:
                lines.append(stripped)

    if not lines:
        return []

    # Auto-detect delimiter from the first lines (\\t if present, else | if present, else ,)
    preview = "\n".join(lines[:20])
    if "\t" in preview:
        delimiter = "\t"
    elif "|" in preview:
        delimiter = "|"
    else:
        delimiter = ","

    reader = csv.reader(lines, delimiter=delimiter)
    try:
        first_row = next(reader)
    except StopIteration:
        return []

    first_row_lower = [c.strip().lower() for c in first_row]
    npa_idx: Optional[int] = None
    nxx_idx: Optional[int] = None
    npa_nxx_idx: Optional[int] = None
    use_idx: Optional[int] = None

    for idx, col in enumerate(first_row_lower):
        if col in ("npa-nxx", "npa_nxx", "npanxx", "npa/nxx", "prefix"):
            npa_nxx_idx = idx
        elif col in ("npa", "area_code", "areacode", "area code"):
            npa_idx = idx
        elif col in ("nxx", "exchange", "central_office", "co"):
            nxx_idx = idx
        elif col in ("use", "line_type", "linetype", "line type", "type", "use_type", "usetype", "use type", "service", "service_type"):
            use_idx = idx

    has_header = False
    if npa_nxx_idx is not None or (npa_idx is not None and nxx_idx is not None):
        has_header = True
    elif any(c in ("npa", "nxx", "use", "line_type", "type") for c in first_row_lower):
        has_header = True

    records: List[Tuple[str, str, str]] = []

    if has_header:
        rows_to_process = reader
    else:
        rows_to_process = [first_row] + list(reader)

    for row in rows_to_process:
        if not row:
            continue

        npa = ""
        nxx = ""
        if has_header:
            if npa_nxx_idx is not None and len(row) > npa_nxx_idx:
                val = row[npa_nxx_idx].strip()
                if "-" in val:
                    parts = val.split("-")
                    if len(parts) == 2:
                        npa, nxx = parts[0].strip(), parts[1].strip()
                elif len(val) == 6 and val.isdigit():
                    npa, nxx = val[:3], val[3:]
            elif npa_idx is not None and nxx_idx is not None and len(row) > max(npa_idx, nxx_idx):
                npa = row[npa_idx].strip()
                nxx = row[nxx_idx].strip()
        else:
            # Positional parsing fallback
            val0 = row[0].strip()
            if "-" in val0:
                parts = val0.split("-")
                if len(parts) == 2:
                    npa, nxx = parts[0].strip(), parts[1].strip()
            elif len(val0) == 6 and val0.isdigit():
                npa, nxx = val0[:3], val0[3:]
            elif len(row) > 1:
                npa = val0
                nxx = row[1].strip()

        if not (len(npa) == 3 and npa.isdigit() and len(nxx) == 3 and nxx.isdigit()):
            continue

        use_val = ""
        if has_header and use_idx is not None and len(row) > use_idx:
            use_val = row[use_idx].strip()
        else:
            # Positional parsing for use: dedicated use column (index 5 or 4, or matching known use tokens)
            # Never scan carrier names (index 1/2) or city names (index 2/3)
            for candidate_idx in (5, 4):
                if len(row) > candidate_idx:
                    cand = row[candidate_idx].strip().upper()
                    if any(tok in cand for tok in ("WIRELESS", "MOBILE", "CELLULAR", "PCS", "CMRS", "VOIP", "PAGING", "PAGER", "WIRELINE", "LANDLINE", "LOCAL", "CLEC", "ILEC", "UA", "AS")):
                        use_val = row[candidate_idx].strip()
                        break
            if not use_val:
                if len(row) > 5:
                    use_val = row[5].strip()
                elif len(row) > 4:
                    use_val = row[4].strip()

        cat = classify_line_type(use_val)
        records.append((npa, nxx, cat))

    return records


def seed_prefixes(db_path: Optional[str] = None, force: bool = False):
    if db_path is None:
        cfg = database.get_default_config()
        db_path = cfg.get("app", {}).get("db_path", "data/lead_pipeline.db")

    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(PROJECT_ROOT, db_path))

    logging.info(f"Connecting to database '{db_path}'...")
    database.init_db(db_path)

    conn = database.get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM phone_prefixes;")
        existing_count = cursor.fetchone()[0]
        if existing_count > 50000 and not force:
            logging.info(f"phone_prefixes table already seeded ({existing_count:,} records). Skipping download.")
            return
    finally:
        conn.close()

    logging.info(f"Downloading official NANPA prefix data from {NANPA_URL}...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    req = urllib.request.Request(NANPA_URL, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            zip_bytes = io.BytesIO(resp.read())
    except Exception as e:
        logging.error(f"Failed to download NANPA dataset: {e}")
        return

    logging.info("Extracting and parsing prefix records in memory...")
    records = []
    with zipfile.ZipFile(zip_bytes) as z:
        for filename in z.namelist():
            if filename.endswith(".txt") or filename.endswith(".csv"):
                with z.open(filename) as f:
                    stream = io.TextIOWrapper(f, encoding="latin-1")
                    file_records = parse_nanpa_stream(stream)
                    records.extend(file_records)

    if not records:
        logging.warning("No valid records found in extracted archive.")
        return

    logging.info(f"Inserting {len(records):,} prefix mappings into SQLite...")
    conn = database.get_connection(db_path)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.executemany(
                "INSERT OR REPLACE INTO phone_prefixes (npa, nxx, line_type) VALUES (?, ?, ?);",
                records
            )
    finally:
        conn.close()

    logging.info("NANPA prefix seeding complete. Offline phone line-type classification is active.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download and seed FCC/NANPA phone prefix database.")
    parser.add_argument("--db-path", dest="db_path", default=None, help="Path to SQLite database file.")
    parser.add_argument("--force", action="store_true", help="Force re-download and re-seed even if table has >50k records.")
    args = parser.parse_args()
    seed_prefixes(db_path=args.db_path, force=args.force)
