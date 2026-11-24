import os
import sys
import json

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from core import classifier

# Load actual production config.json
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


def test_classifier():
    print("=== Testing classifier.py Unit Test Suite ===")

    # 1. Sanitizer Tests
    print("--> Test 1: Sanitizer null-like token rejection...")
    assert classifier.sanitize_text("None") is None
    assert classifier.sanitize_text("N/A") is None
    assert classifier.sanitize_text("  undefined  ") is None
    assert classifier.sanitize_text("[]") is None
    assert classifier.sanitize_text("Acme Roofing") == "Acme Roofing"

    assert classifier.sanitize_email("None") is None
    assert classifier.sanitize_email("contact@acme.com") == "contact@acme.com"
    assert classifier.sanitize_email("not-an-email") is None
    assert classifier.sanitize_email("logo.png") is None

    assert classifier.sanitize_url("None") is None
    assert classifier.sanitize_url("https://acme.com/contact") == "https://acme.com/contact"
    assert classifier.sanitize_url("javascript:void(0)") is None

    assert classifier.sanitize_phone("None") is None
    assert classifier.sanitize_phone("+1 (214) 555-0199") == "+1 (214) 555-0199"
    assert classifier.sanitize_phone("12345") is None  # < 10 digits

    # 2. Gate: Missing Business Name
    print("--> Test 2: Missing business name rejection...")
    st, r, sc = classifier.classify_lead({"business_name": "  "}, {}, CONFIG)
    assert st == "disqualified" and "Missing Business Name" in r

    # 3. Gate: Unaudited Lead Staged As 'new' (Defect 1 Fix)
    print("--> Test 3: Unaudited lead protection (never disqualified)...")
    lead_raw = {
        "business_name": "Apex Commercial",
        "website_url": "https://apex.com",
        "clean_phone": "+12145550199"
    }
    st, r, sc = classifier.classify_lead(lead_raw, {"audit_completed": False}, CONFIG)
    assert st == "new", f"Expected 'new', got '{st}'"
    assert "Pending Audit" in r

    # 4. Gate: Healthy Website Disqualification (Defect 2 Fix)
    print("--> Test 4: Healthy site with 0 flaws is disqualified...")
    audit_healthy = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": [],
        "failed_major_test": 0
    }
    st, r, sc = classifier.classify_lead(lead_raw, audit_healthy, CONFIG)
    assert st == "disqualified", f"Expected 'disqualified', got '{st}'"
    assert "Healthy Website" in r

    # 5. Gate: Flawed Website + Active Mobile Phone -> Outreach Ready
    print("--> Test 5: Flawed website + mobile phone -> outreach_ready...")
    audit_flawed = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": ["broken_mobile", "missing_lead_capture"],
        "failed_major_test": 1
    }
    lead_mobile = {
        "business_name": "Apex Commercial",
        "website_url": "https://apex.com",
        "clean_phone": "+12145550199",
        "line_type": "Mobile",
        "review_count": 25
    }
    st, r, sc = classifier.classify_lead(lead_mobile, audit_flawed, CONFIG)
    assert st == "outreach_ready", f"Expected 'outreach_ready', got '{st}'"
    # Expected score: broken_mobile(3.0) + missing_lead_capture(2.5) + mobile_bonus(1.5) + min(5.0, 25*0.02=0.5) = 7.5
    assert sc == 7.5, f"Expected score 7.5, got {sc}"

    # 6. Gate: Flawed Website + Toll-Free Only (Defect 3 Fix)
    print("--> Test 6: Toll-Free exclusion from outreach when no email exists...")
    lead_toll_free = {
        "business_name": "Corporate Roofing Inc",
        "website_url": "https://corproofing.com",
        "clean_phone": "+18005550199",
        "line_type": "Toll-Free"
    }
    st, r, sc = classifier.classify_lead(lead_toll_free, audit_flawed, CONFIG)
    assert st == "missing_info", f"Expected 'missing_info', got '{st}'"

    # 7. Gate: Flawed Website + Toll-Free + Valid DM Email -> Outreach Ready
    print("--> Test 7: Toll-free lead with valid email passes...")
    lead_toll_free_email = {
        "business_name": "Corporate Roofing Inc",
        "website_url": "https://corproofing.com",
        "clean_phone": "+18005550199",
        "line_type": "Toll-Free",
        "dm_email": "owner@corproofing.com"
    }
    st, r, sc = classifier.classify_lead(lead_toll_free_email, audit_flawed, CONFIG)
    assert st == "outreach_ready", f"Expected 'outreach_ready', got '{st}'"

    # 8. Gate: Dead Domain + Active Phone -> Dead-Site Goldmine
    print("--> Test 8: Dead domain + active phone...")
    audit_dead = {
        "audit_completed": True,
        "is_unreachable": True,
        "flaw_codes": []
    }
    lead_dead_phone = {
        "business_name": "Legacy Roof Repair",
        "website_url": "https://legacyroof.com",
        "clean_phone": "+12145550188",
        "line_type": "Local (US)"
    }
    st, r, sc = classifier.classify_lead(lead_dead_phone, audit_dead, CONFIG)
    assert st == "outreach_ready" and "Dead Domain" in r

    # 9. Gate: Path B (No Website) + Active Phone
    print("--> Test 9: No website + active phone...")
    lead_no_site = {
        "business_name": "Lone Star Shingle",
        "website_url": None,
        "clean_phone": "+12145550188",
        "line_type": "Mobile"
    }
    st, r, sc = classifier.classify_lead(lead_no_site, {"audit_completed": True}, CONFIG)
    assert st == "outreach_ready" and "No Website" in r

    # 10. Gate: Flawed Website + Instagram Direct Contact Passes
    print("--> Test 10: Instagram URL qualifies as actionable contact channel...")
    lead_ig = {
        "business_name": "Modern Metal Roofs",
        "website_url": "https://modernmetalroofs.com",
        "clean_phone": None,
        "instagram_url": "https://instagram.com/modernmetalroofs"
    }
    st, r, sc = classifier.classify_lead(lead_ig, audit_flawed, CONFIG)
    assert st == "outreach_ready", f"Expected 'outreach_ready', got '{st}'"
    assert "Social Available" in r

    # 11. Gate: Flawed Website + WhatsApp URL -> Outreach Ready (Primary Channel WhatsApp)
    print("--> Test 11: WhatsApp URL qualifies as actionable contact channel...")
    lead_wa = {
        "business_name": "Elite Roofing Pros",
        "website_url": "https://eliteroofingpros.com",
        "clean_phone": None,
        "whatsapp_url": "https://wa.me/12145550188"
    }
    st, r, sc = classifier.classify_lead(lead_wa, audit_flawed, CONFIG)
    assert st == "outreach_ready", f"Expected 'outreach_ready', got '{st}'"
    assert "WhatsApp Available" in r, f"Expected 'WhatsApp Available' in reason, got '{r}'"

    # 12. Scoring: WhatsApp Bonus is dynamically added
    print("--> Test 12: Priority score includes has_whatsapp_bonus...")
    score_without_wa = classifier.calculate_priority_score({"clean_phone": None}, audit_flawed, CONFIG)
    score_with_wa = classifier.calculate_priority_score({"whatsapp_url": "https://wa.me/12145550188"}, audit_flawed, CONFIG)
    expected_bonus = CONFIG["queue"]["ranking_weights"]["has_whatsapp_bonus"]
    assert round(score_with_wa - score_without_wa, 2) == expected_bonus, f"Expected {expected_bonus} bonus, got {score_with_wa - score_without_wa}"

    print("\nALL CLASSIFIER UNIT TESTS PASSED (100%)!")


if __name__ == "__main__":
    test_classifier()
