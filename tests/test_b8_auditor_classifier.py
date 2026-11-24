import json
import os
import pytest
from core import auditor, classifier

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_p2_m05_config_ranking_weights():
    """Verify dead keys are pruned and render_incomplete is wired to 0.0."""
    cfg = load_config()
    weights = cfg["queue"]["ranking_weights"]
    
    assert "high_ttfb" not in weights, "high_ttfb should be removed from ranking_weights"
    assert "obstructive_header" not in weights, "obstructive_header should be removed from ranking_weights"
    assert weights.get("render_incomplete") == 0.0, "render_incomplete weight should be 0.0"
    assert weights.get("server_delay") == 1.5
    assert weights.get("obstructive_sticky_header") == 2.0


def test_p1_m13_parse_json_ld_mailto_normalization():
    """Verify parse_json_ld strips mailto:, query strings, unquotes, and lowercases."""
    html = """
    <html>
    <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "LocalBusiness",
            "name": "Apex Roofing",
            "email": "mailto:Owner%20Bob@ApexRoofingTX.com?subject=Inquiry",
            "contactPoint": [
                {
                    "@type": "ContactPoint",
                    "email": "mailto:support@apexroofingtx.com?body=hello"
                }
            ]
        }
        </script>
    </head>
    <body><h1>Test</h1></body>
    </html>
    """
    data = auditor.parse_json_ld(html)
    assert "owner bob@apexroofingtx.com" in data["emails"]
    assert "support@apexroofingtx.com" in data["emails"]
    # Check that mailto: and query strings were stripped
    for email in data["emails"]:
        assert not email.startswith("mailto:")
        assert "?" not in email
        assert email == email.lower()


def test_p1_m13_json_ld_noise_filtering():
    """Verify JSON-LD noise emails are filtered out during merge in auditor."""
    generic_prefixes = ["info@", "contact@"]
    noise_tokens = ["sentry", "wixpress", "example.com"]
    
    raw_jsonld = ["mailto:clean.owner@roofing.com", "bad@wixpress.com", "test@example.com"]
    j_dm, j_gen = auditor.extract_emails(" ".join(raw_jsonld), generic_prefixes, noise_tokens=noise_tokens)
    
    dm_emails = ["scraped@roofing.com"]
    merged_dm = list(dict.fromkeys(j_dm + dm_emails))
    
    assert "clean.owner@roofing.com" in merged_dm
    assert "scraped@roofing.com" in merged_dm
    assert "bad@wixpress.com" not in merged_dm
    assert "test@example.com" not in merged_dm


def test_p1_m13_social_links_facebook_and_youtube():
    """Verify Facebook /p/ is allowed and YouTube routes are properly categorized."""
    links = [
        # Facebook
        "https://www.facebook.com/p/Apex-Roofing-Solutions-100063548291/",
        "https://www.facebook.com/share/somepost123",
        "https://www.facebook.com/sharer/sharer.php?u=https://example.com",
        
        # YouTube - channels & vanities
        "https://www.youtube.com/apexroofingtx",
        "https://www.youtube.com/@apexroofingtx",
        "https://www.youtube.com/c/ApexRoofingTX",
        "https://www.youtube.com/channel/UC1234567890abcdef",
        
        # YouTube - non-channel routes (should be rejected)
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abcdef12345",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL123456",
        "https://www.youtube.com/feed/explore",
        
        # Instagram post (should still be excluded)
        "https://www.instagram.com/p/C3m2Xyz123/"
    ]
    
    socials = auditor.extract_social_links(links, business_name="Apex Roofing", clean_domain="apexroofingtx.com")
    
    # Facebook should accept /p/ business page, not /share/
    assert socials["facebook_url"] == "https://www.facebook.com/p/Apex-Roofing-Solutions-100063548291"
    
    # Instagram post should still be ignored
    assert socials["instagram_url"] is None
    
    # YouTube should pick a valid channel/vanity and never a watch link or youtu.be
    assert socials["youtube_url"] in [
        "https://www.youtube.com/apexroofingtx",
        "https://www.youtube.com/@apexroofingtx",
        "https://www.youtube.com/c/ApexRoofingTX",
        "https://www.youtube.com/channel/UC1234567890abcdef"
    ]


def test_p1_m14_render_incomplete_classifier_routing():
    """Verify render_incomplete in flaw_codes routes to missing_info even with actionable contacts."""
    cfg = load_config()
    lead = {
        "business_name": "Lone Star Commercial Roofing",
        "website_url": "https://lonestarroofing.com",
        "clean_phone": "+12145550199",
        "dm_email": "owner@lonestarroofing.com",
        "generic_email": "info@lonestarroofing.com",
        "line_type": "Mobile"
    }
    
    audit_data = {
        "audit_completed": True,
        "is_unreachable": False,
        "dom_nodes": 45,
        "text_length": 80,
        "flaw_codes": ["render_incomplete"],
        "flaw_labels": ["Render Incomplete / Blocked Shell (Manual Verification Required)"],
        "failed_major_test": 1
    }
    
    status, reason, score = classifier.classify_lead(lead, audit_data, cfg)
    assert status == "missing_info"
    assert "Render Incomplete" in reason
    assert score == 0.0


def test_p1_m14_healthy_vs_flawed_without_render_incomplete():
    """Verify normal leads still classify properly when render_incomplete is absent."""
    cfg = load_config()
    lead = {
        "business_name": "Lone Star Commercial Roofing",
        "website_url": "https://lonestarroofing.com",
        "clean_phone": "+12145550199",
        "dm_email": "owner@lonestarroofing.com",
        "line_type": "Mobile"
    }
    
    # Flawed site with actionable contact -> outreach_ready
    flawed_audit = {
        "audit_completed": True,
        "is_unreachable": False,
        "dom_nodes": 500,
        "text_length": 1200,
        "flaw_codes": ["broken_mobile", "slow_load"],
        "failed_major_test": 1
    }
    status, reason, score = classifier.classify_lead(lead, flawed_audit, cfg)
    assert status == "outreach_ready"
    assert score > 0.0
