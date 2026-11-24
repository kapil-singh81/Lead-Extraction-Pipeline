import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core import database
from scripts.seed_nanpa import parse_nanpa_stream, seed_prefixes, classify_line_type
from scripts.inspect_db import inspect_database


def test_classify_line_type_rules():
    """Verify classification strictly from Use column tokens."""
    # Mobile
    assert classify_line_type("WIRELESS") == "Mobile"
    assert classify_line_type("mobile") == "Mobile"
    assert classify_line_type("CELLULAR") == "Mobile"
    assert classify_line_type("PCS") == "Mobile"
    assert classify_line_type("CMRS") == "Mobile"

    # VoIP
    assert classify_line_type("VOIP") == "VoIP"
    assert classify_line_type("Nomadic VoIP") == "VoIP"

    # Paging
    assert classify_line_type("PAGING") == "Paging"
    assert classify_line_type("PAGER") == "Paging"

    # Landline
    assert classify_line_type("WIRELINE") == "Landline"
    assert classify_line_type("LANDLINE") == "Landline"
    assert classify_line_type("LOCAL") == "Landline"
    assert classify_line_type("CLEC") == "Landline"
    assert classify_line_type("ILEC") == "Landline"
    assert classify_line_type("UA") == "Landline"
    assert classify_line_type("AS") == "Landline"

    # Other / Empty
    assert classify_line_type("") == "Other"
    assert classify_line_type("UNKNOWN") == "Other"
    assert classify_line_type("SPECIAL") == "Other"


def test_p4_m03_nanpa_delimiter_and_header_parsing():
    """
    P4-M03:
    1. parse_nanpa_stream() handles tab-delimited headers with combined NPA-NXX:
       "214-555\tMobile Telecom\tMobile\tAL\tWIRELINE" -> ("214", "555", "Landline"), NOT "Mobile"!
    2. Comma-delimited separate NPA,NXX,...,Use rows.
    3. Proper mapping for PAGING -> Paging, WIRELESS -> Mobile, VOIP -> VoIP.
    """
    # 1. Tab-delimited header with combined NPA-NXX and 'Mobile' in carrier/city names
    tab_data_with_header = (
        "NPA-NXX\tCarrier\tCity\tState\tUse\n"
        "214-555\tMobile Telecom\tMobile\tAL\tWIRELINE\n"
        "972-234\tDallas Wireless LLC\tDallas\tTX\tWIRELESS\n"
    )
    records_tab = parse_nanpa_stream(tab_data_with_header)
    assert len(records_tab) == 2
    # Crucial test: 'Mobile Telecom' and 'Mobile, AL' must NOT make this Mobile
    assert records_tab[0] == ("214", "555", "Landline")
    assert records_tab[1] == ("972", "234", "Mobile")

    # 2. Tab-delimited WITHOUT header (positional fallback)
    tab_data_no_header = (
        "214-555\tMobile Telecom\tMobile\tAL\tWIRELINE\n"
        "469-888\tVoip Systems\tPlano\tTX\tVOIP\n"
    )
    records_no_header = parse_nanpa_stream(tab_data_no_header)
    assert len(records_no_header) == 2
    assert records_no_header[0] == ("214", "555", "Landline")
    assert records_no_header[1] == ("469", "888", "VoIP")

    # 3. Comma-delimited with separate columns
    comma_data = (
        "NPA,NXX,Company,State,Use\n"
        "214,555,Acme Paging,TX,PAGING\n"
        "972,555,Cell Co,TX,WIRELESS\n"
        "469,555,Cloud PBX,TX,VOIP\n"
        "817,555,Local Tel,TX,WIRELINE\n"
        "512,555,Unassigned,TX,UA\n"
        "210,555,Mystery Co,TX,UNKNOWN\n"
    )
    records_comma = parse_nanpa_stream(comma_data)
    assert len(records_comma) == 6
    assert records_comma[0] == ("214", "555", "Paging")
    assert records_comma[1] == ("972", "555", "Mobile")
    assert records_comma[2] == ("469", "555", "VoIP")
    assert records_comma[3] == ("817", "555", "Landline")
    assert records_comma[4] == ("512", "555", "Landline")
    assert records_comma[5] == ("210", "555", "Other")

    # 4. Pipe-delimited stream with TextIO stream input
    pipe_stream = io.StringIO(
        "NPA|NXX|Company|State|Line_Type\n"
        "682|555|Pager Plus|TX|PAGER\n"
        "832|555|Cellular One|TX|CELLULAR\n"
    )
    records_pipe = parse_nanpa_stream(pipe_stream)
    assert records_pipe == [
        ("682", "555", "Paging"),
        ("832", "555", "Mobile"),
    ]

    # 5. Invalid/short rows safely skipped
    messy_data = "NPA,NXX,Use\nINVALID\n12,34,WIRELESS\n214,abc,WIRELESS\n214,555,WIRELESS"
    assert parse_nanpa_stream(messy_data) == [("214", "555", "Mobile")]

    # 6. Empty stream returns empty list
    assert parse_nanpa_stream("") == []
    assert parse_nanpa_stream([]) == []


def test_p4_m02_seed_prefixes_force_flag_and_skip(tmp_path: Path):
    """
    P4-m02:
    seed_prefixes(db_path=..., force=False) skips when >50k rows exist (mocking count/download)
    and proceeds when force=True.
    """
    test_db = str(tmp_path / "b25_prefixes.db")
    database.init_db(test_db)

    conn = database.get_connection(test_db)
    with conn:
        cur = conn.cursor()
        # Seed 50,001 dummy rows to test threshold
        dummy_rows = [(f"{100 + i // 1000}", f"{i % 1000:03d}", "Landline") for i in range(50001)]
        cur.executemany("INSERT INTO phone_prefixes (npa, nxx, line_type) VALUES (?, ?, ?);", dummy_rows)
    conn.close()

    # 1. Call without force -> Must skip download
    with patch("urllib.request.urlopen") as mock_urlopen:
        seed_prefixes(db_path=test_db, force=False)
        mock_urlopen.assert_not_called()

    # 2. Call with force=True -> Must trigger download and seed records
    mock_csv_content = (
        "NPA,NXX,Company,State,Use\n"
        "214,999,Forced Mobile Co,TX,WIRELESS\n"
    ).encode("latin-1")

    # Create in-memory zip containing allutlzd.txt
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as z:
        z.writestr("allutlzd.txt", mock_csv_content)
    zip_buffer.seek(0)

    mock_resp = MagicMock()
    mock_resp.read.return_value = zip_buffer.getvalue()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        seed_prefixes(db_path=test_db, force=True)
        mock_urlopen.assert_called_once()

    # Verify newly seeded prefix exists in SQLite
    conn = database.get_connection(test_db)
    cur = conn.cursor()
    cur.execute("SELECT line_type FROM phone_prefixes WHERE npa = '214' AND nxx = '999';")
    row = cur.fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "Mobile"


def test_p4_m01_inspect_db_clean_import_and_guards(tmp_path: Path, capsys):
    """
    P4-m01:
    1. Importing scripts.inspect_db runs zero queries.
    2. Calling inspect_database(str(tmp_path / "missing.db")) prints a message without creating missing.db.
    3. Calling it on a valid initialized tmp_path DB prints all 5 sections and closes cleanly.
    """
    # 1. Clean import verification
    import scripts.inspect_db
    assert hasattr(scripts.inspect_db, "inspect_database")

    # 2. Calling on non-existent DB prints message and does NOT create file
    missing_db = tmp_path / "missing_lead_pipeline.db"
    assert not missing_db.exists()

    inspect_database(str(missing_db))
    out = capsys.readouterr().out
    assert "Database file not found:" in out
    assert not missing_db.exists(), "inspect_database must not create an empty DB when file does not exist"

    # 3. Calling on valid initialized DB
    valid_db = str(tmp_path / "valid_lead_pipeline.db")
    database.init_db(valid_db)

    # Populate sample lead and outreach entry
    lead_id = database.insert_lead({
        "place_id": "ChIJ_inspect_01",
        "business_name": "Apex Test Contracting",
        "clean_phone": "+12145551234",
        "status": "contacted",
        "priority_score": 90.0
    }, db_path=valid_db)

    database.log_outreach(
        lead_id=lead_id,
        channel="whatsapp",
        outcome="sent",
        message_sent="Test outreach message",
        notes="Inspected note",
        db_path=valid_db
    )

    inspect_database(valid_db)
    out_valid = capsys.readouterr().out

    # Verify all 5 report sections are printed
    assert "=== CONTACTED LEADS IN DB ===" in out_valid
    assert "=== OUTREACH LOG ROWS ===" in out_valid
    assert "=== STATUS COUNTS IN LEADS ===" in out_valid
    assert "=== CHANNEL COUNTS IN OUTREACH LOG ===" in out_valid
    assert "=== DISTINCT LEADS IN OUTREACH LOG PER CHANNEL ===" in out_valid
    assert "Apex Test Contracting" in out_valid
    assert "Inspected note" in out_valid
