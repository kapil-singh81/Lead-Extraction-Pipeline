import os
import sys
import json
import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import classifier

# Load production config
with open(os.path.join(PROJECT_ROOT, "config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


def test_p1_m10_dead_domain_email_available():
    """Test 1: Dead domain lead with dm_email and no phone -> outreach_ready, Dead Domain (Email Available), score = 5.0 + 2.0 = 7.0."""
    audit_data = {
        "audit_completed": True,
        "is_unreachable": True,
        "flaw_codes": ["dead_domain"]
    }
    lead = {
        "business_name": "Ghost Roofing Co",
        "website_url": "https://ghostroofing-dead.com",
        "clean_phone": None,
        "dm_email": "owner@ghostroofing.com",
        "review_count": 0
    }
    st, reason, score = classifier.classify_lead(lead, audit_data, CONFIG)
    assert st == "outreach_ready"
    assert reason == "Dead Domain (Email Available)"
    assert score == 7.0  # 5.0 base + 2.0 has_email_bonus


def test_p1_m10_no_website_review_and_mobile_bonuses():
    """Test 2: No-website lead with review_count=150, line_type='Mobile', valid phone scores higher than 0-review local-phone lead (8.5 vs 4.0)."""
    audit_data = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": ["no_website"]
    }
    lead_high_authority = {
        "business_name": "Titan Roofing Services",
        "website_url": None,
        "clean_phone": "+12145550199",
        "line_type": "Mobile",
        "review_count": 150
    }
    lead_baseline = {
        "business_name": "Basic Roofing Services",
        "website_url": None,
        "clean_phone": "+12145550199",
        "line_type": "Local (US)",
        "review_count": 0
    }
    st_high, reason_high, score_high = classifier.classify_lead(lead_high_authority, audit_data, CONFIG)
    st_base, reason_base, score_base = classifier.classify_lead(lead_baseline, audit_data, CONFIG)

    assert st_high == "outreach_ready"
    assert reason_high == "No Website (Phone Available)"
    # Base 4.0 + 1.5 (mobile bonus) + 3.0 (150 reviews * 0.02 = 3.0) = 8.5
    assert score_high == 8.5

    assert st_base == "outreach_ready"
    assert reason_base == "No Website (Phone Available)"
    assert score_base == 4.0  # Base 4.0 + 0.0 bonuses

    assert score_high > score_base


def test_p1_m10_no_website_social_available():
    """Test 3: No-website lead with facebook_url and no phone -> outreach_ready, No Website (Social Available)."""
    audit_data = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": ["no_website"]
    }
    lead = {
        "business_name": "Social First Roofing",
        "website_url": None,
        "clean_phone": None,
        "facebook_url": "https://facebook.com/socialfirstroofing"
    }
    st, reason, score = classifier.classify_lead(lead, audit_data, CONFIG)
    assert st == "outreach_ready"
    assert reason == "No Website (Social Available)"
    assert score == 4.0


def test_p1_m10_no_contact_disqualification_and_missing_info():
    """Verify dead domain without contact is disqualified and no-website without contact is missing_info."""
    audit_dead = {
        "audit_completed": True,
        "is_unreachable": True,
        "flaw_codes": ["dead_domain"]
    }
    lead_dead_no_contact = {
        "business_name": "Empty Dead Roof",
        "website_url": "https://emptydeadroof.com",
        "clean_phone": None
    }
    st_dead, reason_dead, score_dead = classifier.classify_lead(lead_dead_no_contact, audit_dead, CONFIG)
    assert st_dead == "disqualified"
    assert reason_dead == "Dead Domain (No Contact)"
    assert score_dead == 0.0

    audit_no_web = {
        "audit_completed": True,
        "is_unreachable": False,
        "flaw_codes": ["no_website"]
    }
    lead_no_web_no_contact = {
        "business_name": "Empty No Web Roof",
        "website_url": None,
        "clean_phone": None
    }
    st_web, reason_web, score_web = classifier.classify_lead(lead_no_web_no_contact, audit_no_web, CONFIG)
    assert st_web == "missing_info"
    assert reason_web == "No Website & No Actionable Contact"
    assert score_web == 0.0


def test_p1_m11_sanitize_email_expanded_blacklist():
    """Test 4: sanitize_email() rejects .jpeg, .svg, .gif, .png, .jpg, .webp, .js, .css and accepts valid emails."""
    # Invalid asset/code pseudo-emails
    assert classifier.sanitize_email("banner.jpeg") is None
    assert classifier.sanitize_email("icon.svg") is None
    assert classifier.sanitize_email("spinner.gif") is None
    assert classifier.sanitize_email("logo.png") is None
    assert classifier.sanitize_email("hero.jpg") is None
    assert classifier.sanitize_email("thumb.webp") is None
    assert classifier.sanitize_email("script.js") is None
    assert classifier.sanitize_email("main.css") is None

    # Valid emails
    assert classifier.sanitize_email("contact@business.com") == "contact@business.com"
    assert classifier.sanitize_email("JOHN.DOE@ROOFING.NET") == "john.doe@roofing.net"
    assert classifier.sanitize_email("sales+lead@my-company.com") == "sales+lead@my-company.com"
