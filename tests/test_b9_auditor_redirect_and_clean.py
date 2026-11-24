import base64
import json
import os
from unittest.mock import patch, MagicMock
import pytest
from core import auditor

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_p1_m08_decode_bing_redirect():
    """Verify decode_bing_redirect decodes valid Bing click redirects and ignores normal URLs."""
    # Test valid URL encoding (https://example.com/ -> aHR0cHM6Ly9leGFtcGxlLmNvbS8)
    encoded_val = base64.urlsafe_b64encode(b"https://example.com/").decode("ascii").rstrip("=")
    bing_redirect = f"https://www.bing.com/ck/a?!&&p=abc123xyz&u=a1{encoded_val}&ntb=1"
    
    decoded = auditor.decode_bing_redirect(bing_redirect)
    assert decoded == "https://example.com/"

    # Test with standard sample from user prompt
    sample_redirect = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8"
    assert auditor.decode_bing_redirect(sample_redirect) == "https://example.com/"

    # Test non-Bing or non-redirect URLs are untouched
    normal_url = "https://apexroofingtx.com"
    assert auditor.decode_bing_redirect(normal_url) == normal_url

    bing_search = "https://www.bing.com/search?q=roofing+dallas"
    assert auditor.decode_bing_redirect(bing_search) == bing_search

    # Test empty or malformed inputs
    assert auditor.decode_bing_redirect("") == ""
    assert auditor.decode_bing_redirect(None) is None
    malformed_bing = "https://www.bing.com/ck/a?u=a1not_valid_base64!!!"
    assert auditor.decode_bing_redirect(malformed_bing) == malformed_bing


def test_p1_m08_recover_missing_website_with_bing_redirect():
    """Verify recover_missing_website decodes bing.com/ck/a redirects rather than dropping them as blacklisted bing.com."""
    encoded_site = base64.urlsafe_b64encode(b"https://lonestarcommercialroofing.com/").decode("ascii").rstrip("=")
    bing_redirect = f"https://www.bing.com/ck/a?!&&p=test&u=a1{encoded_site}&ntb=1"

    fake_html = f"""
    <html>
      <body>
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="{bing_redirect}">Lone Star Commercial Roofing - Official Site</a></h2>
          </li>
        </ol>
      </body>
    </html>
    """.encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_html
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        recovered = auditor.recover_missing_website(
            business_name="Lone Star Commercial Roofing",
            city="Dallas TX",
            excluded_domains=["yelp.com", "facebook.com"]
        )

    assert recovered == "https://lonestarcommercialroofing.com/"


def test_p1_m09_extract_social_links_niche_agnostic():
    """Verify extract_social_links uses niche-agnostic stop tokens and does not drop roofing/construction."""
    links = [
        "https://www.facebook.com/apexroofingsolutions",
        "https://www.facebook.com/unrelateddirectory"
    ]
    # 'roofing' should be preserved as a target matching token
    socials = auditor.extract_social_links(links, business_name="Apex Roofing Solutions LLC", clean_domain="apexroofing.com")
    assert socials["facebook_url"] == "https://www.facebook.com/apexroofingsolutions"

    # Non-roofing businesses (e.g. dental / law) correctly retain their core tokens
    dental_links = [
        "https://www.facebook.com/carterinjurylaw",
        "https://www.facebook.com/randomlawfirm"
    ]
    dental_socials = auditor.extract_social_links(
        dental_links,
        business_name="Carter Injury Law Group PLLC",
        clean_domain="carterinjury.com"
    )
    assert dental_socials["facebook_url"] == "https://www.facebook.com/carterinjurylaw"

    # Custom stop tokens override
    custom_stop = ["solutions", "llc"]
    custom_socials = auditor.extract_social_links(
        links,
        business_name="Apex Roofing Solutions LLC",
        clean_domain="apex.com",
        stop_tokens=custom_stop
    )
    assert custom_socials["facebook_url"] == "https://www.facebook.com/apexroofingsolutions"


def test_p1_m09_config_and_auditor_trust_keywords():
    """Verify trust_keywords and social_stop_tokens are in config and not duplicated as hardcoded lists in auditor.py."""
    cfg = load_config()
    auditor_cfg = cfg.get("auditor", {})

    assert "trust_keywords" in auditor_cfg, "trust_keywords must exist in config.json under auditor"
    trust_kw = auditor_cfg["trust_keywords"]
    assert len(trust_kw) >= 18
    assert "insured" in trust_kw
    assert "licensed" in trust_kw
    assert "warranty" in trust_kw

    assert "social_stop_tokens" in auditor_cfg, "social_stop_tokens must exist in config.json under auditor"
    stop_tokens = auditor_cfg["social_stop_tokens"]
    assert "llc" in stop_tokens
    assert "inc" in stop_tokens
    assert "roofing" not in stop_tokens
    assert "construction" not in stop_tokens

    # Verify auditor.py source code has zero hardcoded duplicate lists for trust_keywords
    with open(os.path.join(PROJECT_ROOT, "core", "auditor.py"), "r", encoding="utf-8") as f:
        auditor_source = f.read()

    # The hardcoded array in evaluate strings or subpage should not exist
    assert "const trustKeywords = [" not in auditor_source, "Hardcoded JS trustKeywords array must be removed"
    assert "trust_keywords = [\n                            'insured'" not in auditor_source, "Hardcoded subpage trust_keywords must be removed"
    assert '"roofing", "construction"' not in auditor_source, "Hardcoded niche tokens 'roofing', 'construction' must be removed"
