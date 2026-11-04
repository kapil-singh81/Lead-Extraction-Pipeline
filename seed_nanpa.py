"""
seed_nanpa.py - Downloads and seeds the official, free FCC/NANPA public NPA-NXX
telecom table into SQLite for 100% offline, zero-cost US line-type classification.
"""

import io
import re
import csv
import zipfile
import logging
import urllib.request
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NANPA_URL = "https://www.nationalnanpa.com/nanp1/allutlzd.zip"


def seed_prefixes(db_path: str = "lead_pipeline.db"):
    logging.info(f"Connecting to database '{db_path}'...")
    database.init_db(db_path)

    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS phone_prefixes (
                npa TEXT,
                nxx TEXT,
                line_type TEXT,
                PRIMARY KEY (npa, nxx)
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone_prefix_lookup ON phone_prefixes(npa, nxx);")
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM phone_prefixes;")
        existing_count = cursor.fetchone()[0]
        if existing_count > 50000:
            logging.info(f"phone_prefixes table already seeded ({existing_count:,} records). Skipping download.")
            return

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
                    reader = csv.reader(io.TextIOWrapper(f, encoding="latin-1"))
                    for row in reader:
                        if len(row) < 5:
                            continue
                        npa, nxx = row[0].strip(), row[1].strip()
                        if not (len(npa) == 3 and npa.isdigit() and len(nxx) == 3 and nxx.isdigit()):
                            continue

                        # Column 4/5 indicates use: 'WIRELESS', 'WIRELINE', 'VOIP', 'PAGING'
                        use_type = " ".join(row).upper()
                        if "WIRELESS" in use_type or "MOBILE" in use_type or "CELLULAR" in use_type:
                            cat = "Mobile"
                        elif "VOIP" in use_type:
                            cat = "VoIP"
                        else:
                            cat = "Landline"

                        records.append((npa, nxx, cat))

    if not records:
        logging.warning("No valid records found in extracted archive.")
        return

    logging.info(f"Inserting {len(records):,} prefix mappings into SQLite...")
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT OR REPLACE INTO phone_prefixes (npa, nxx, line_type) VALUES (?, ?, ?);",
            records
        )
        conn.commit()

    logging.info("NANPA prefix seeding complete. Offline phone line-type classification is active.")


if __name__ == "__main__":
    seed_prefixes()

