import re
from pathlib import Path


def test_p2_m12_dark_mode_table_row_hover_and_no_important():
    """
    P2-M12:
    .table-lead-row:hover td no longer forces #f1f5f9 !important,
    and html.dark .table-lead-row:hover td applies #141414 cleanly.
    """
    css_path = Path("static/style.css")
    assert css_path.exists(), "static/style.css must exist"
    css_content = css_path.read_text(encoding="utf-8")

    # Match .table-lead-row:hover td { ... }
    light_match = re.search(r"\.table-lead-row:hover\s+td\s*\{([^}]+)\}", css_content)
    assert light_match is not None, "Expected .table-lead-row:hover td rule in style.css"
    light_body = light_match.group(1)
    assert "!important" not in light_body, "Must not force !important on light mode table row hover"
    assert "background: #f1f5f9;" in light_body or "background:#f1f5f9;" in light_body.replace(" ", "")

    # Match html.dark .table-lead-row:hover td { ... }
    dark_match = re.search(r"html\.dark\s+\.table-lead-row:hover\s+td\s*\{([^}]+)\}", css_content)
    assert dark_match is not None, "Expected html.dark .table-lead-row:hover td rule in style.css"
    dark_body = dark_match.group(1)
    assert "background: #141414;" in dark_body or "background:#141414;" in dark_body.replace(" ", "")


def test_p2_m09_no_undefined_variables_bg_hover_or_primary():
    """
    P2-m09:
    Zero occurrences of undefined var(--bg-hover) or var(--primary) remain in static/style.css.
    """
    css_path = Path("static/style.css")
    assert css_path.exists(), "static/style.css must exist"
    css_content = css_path.read_text(encoding="utf-8")

    assert "--bg-hover" not in css_content, "Undefined var(--bg-hover) found in static/style.css"
    # Match var(--primary) but allow var(--accent-primary)
    primary_matches = re.findall(r"var\(\s*--primary\s*\)", css_content)
    assert len(primary_matches) == 0, f"Found undefined var(--primary) occurrences: {primary_matches}"

    # Verify .btn-add-channel-plus:hover uses var(--bg-subtle) and var(--accent-primary)
    btn_match = re.search(r"\.btn-add-channel-plus:hover\s*\{([^}]+)\}", css_content)
    assert btn_match is not None, "Expected .btn-add-channel-plus:hover in static/style.css"
    btn_body = btn_match.group(1)
    assert "var(--bg-subtle)" in btn_body
    assert "var(--accent-primary)" in btn_body


def test_p2_m10_keyframes_fadein_defined():
    """
    P2-m10:
    @keyframes fadeIn is defined in static/style.css.
    """
    css_path = Path("static/style.css")
    assert css_path.exists(), "static/style.css must exist"
    css_content = css_path.read_text(encoding="utf-8")

    assert "@keyframes fadeIn" in css_content
    # Check that from and to frames exist
    match = re.search(r"@keyframes\s+fadeIn\s*\{([\s\S]+?)\n\}", css_content)
    assert match is not None, "@keyframes fadeIn block must be well-formed"
    block = match.group(1)
    assert "opacity: 0" in block
    assert "opacity: 1" in block


def test_p2_m12_history_list_declared_only_once():
    """
    P2-m12:
    .history-list { appears only once in static/style.css.
    """
    css_path = Path("static/style.css")
    assert css_path.exists(), "static/style.css must exist"
    css_content = css_path.read_text(encoding="utf-8")

    matches = re.findall(r"(?m)^\.history-list\s*\{", css_content)
    assert len(matches) == 1, f"Expected .history-list {{ to appear exactly once, but found {len(matches)} times"
