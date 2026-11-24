import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.pitch_generator import (
    generate_pitches,
    clean_city_name,
    clean_contractor_name,
    get_proof_asset,
    get_flaw_variants,
    PRIORITY_ORDER
)


def test_clean_contractor_name():
    # Strips tagline and location separators
    assert clean_contractor_name("FOX Roofing | Dallas") == "FOX Roofing"
    assert clean_contractor_name("Acme Roofing – Plano TX") == "Acme Roofing"
    assert clean_contractor_name("Summit Roofing — Dallas TX") == "Summit Roofing"
    assert clean_contractor_name("Apex Roofing - Fort Worth") == "Apex Roofing"

    # Strips legal suffixes
    assert clean_contractor_name("Titan Roofing LLC") == "Titan Roofing"
    assert clean_contractor_name("Superior One Roofing & Construction, Inc.") == "Superior One Roofing & Construction"
    assert clean_contractor_name("Precision Roofers Corp.") == "Precision Roofers"

    # Preserves brand names without separators
    assert clean_contractor_name("Bare Roofing Dallas") == "Bare Roofing Dallas"
    assert clean_contractor_name("Stamper Roofing & Construction") == "Stamper Roofing & Construction"


def test_get_proof_asset():
    # Performance flaws get screen recordings
    for code in ["slow_load", "server_delay", "high_ttfb", "heavy_page", "builder_bloat"]:
        assert get_proof_asset(code) == "15-second screen recording"

    # Visual, layout, content, and dead flaws get screenshots
    for code in ["broken_mobile", "unclickable_phone", "missing_lead_capture", "obstructive_sticky_header", "zero_visual_proof", "dead_domain", "no_website"]:
        assert get_proof_asset(code) == "quick screenshot"


def test_get_flaw_variants_coverage():
    all_codes = [
        "broken_mobile", "unclickable_phone", "missing_lead_capture",
        "obstructive_sticky_header", "slow_load", "server_delay", "high_ttfb",
        "heavy_page", "builder_bloat", "zero_visual_proof", "zero_trust_anchors",
        "thin_architecture", "content_starved"
    ]
    sample_audit = {
        "load_time_sec": 9.6,
        "ttfb_ms": 1867,
        "page_weight_mb": 4.8
    }

    for code in all_codes:
        variants = get_flaw_variants(code, sample_audit)
        for expected_key in ["curiosity", "direct", "impact", "router", "contrast", "preview_hook", "business_effect"]:
            assert expected_key in variants, f"Missing key {expected_key} in variants for {code}"
            val = variants[expected_key]
            assert val and isinstance(val, str), f"Empty variant for {code}.{expected_key}"
            assert "—" not in val, f"Em-dash found in variant {code}.{expected_key}"
        assert len(variants["preview_hook"]) <= 45, f"preview_hook too long for {code}: {variants['preview_hook']}"


def test_rounded_metrics():
    # 9.6s rounds to ~10 seconds
    v_slow = get_flaw_variants("slow_load", {"load_time_sec": 9.6})
    assert "~10 seconds" in v_slow["curiosity"]
    assert "~10 seconds" in v_slow["direct"]

    # 1867ms rounds to ~2 seconds
    v_server = get_flaw_variants("server_delay", {"ttfb_ms": 1867})
    assert "~2 seconds" in v_server["curiosity"]
    assert "~2 seconds" in v_server["direct"]

    # 4.8MB rounds to ~5MB
    v_heavy = get_flaw_variants("heavy_page", {"page_weight_mb": 4.8})
    assert "~5MB" in v_heavy["curiosity"]
    assert "~5MB" in v_heavy["direct"]


def test_fox_roofing_pitches():
    fox_lead = {
        "id": 94,
        "business_name": "FOX Roofing | Dallas",
        "city": "Dallas",
        "niche": "commercial roofer",
        "rating": 5.0,
        "review_count": 75,
        "clean_phone": "+1 214-505-3374",
        "website_url": "https://foxroofing.co/",
        "audit": {
            "flaw_codes": ["slow_load"],
            "load_time_sec": 9.6
        }
    }
    pitches = generate_pitches(fox_lead)

    # 8 approaches
    expected_keys = [
        "naive_curiosity", "loss_aversion", "accusation_audit", "no_oriented",
        "local_contrast", "reputation_dissonance", "gatekeeper", "reciprocity"
    ]
    for k in expected_keys:
        assert k in pitches["approaches"]
        text = pitches["approaches"][k]["text"]
        assert text, f"Empty text for approach {k}"
        assert "—" not in text, f"Em-dash found in {k}"
        assert "http" not in text, f"Link found in {k}"
        assert pitches["approaches"][k]["word_count"] <= 65

    # 4 slots
    assert "slot_1" in pitches["slots"]
    assert "slot_2" in pitches["slots"]
    assert "slot_3" in pitches["slots"]
    assert "slot_4" in pitches["slots"]

    # Clean name used everywhere
    for k, app in pitches["approaches"].items():
        assert "FOX Roofing | Dallas" not in app["text"]
        assert "FOX Roofing" in app["text"]

    # Preview window openings
    assert pitches["approaches"]["naive_curiosity"]["text"].startswith("Is it supposed to")
    assert pitches["approaches"]["loss_aversion"]["text"].startswith("Leads are hitting")

    # Business effect inclusion
    effect = "Most people hit back after 3 seconds and call the next listing."
    assert effect in pitches["approaches"]["loss_aversion"]["text"]
    assert effect in pitches["approaches"]["local_contrast"]["text"]
    assert effect in pitches["approaches"]["reputation_dissonance"]["text"]

    # Reciprocity and Accusation Audit wiring
    assert "so you stop losing mobile visitors" in pitches["approaches"]["reciprocity"]["text"]
    assert "on mobile when checking FOX Roofing" in pitches["approaches"]["accusation_audit"]["text"]

    # Review count >= 30 triggers competitive claim in local_contrast
    assert "even though your reviews beat half the Dallas listings next to you." in pitches["approaches"]["local_contrast"]["text"]
    assert "Spotted this on FOX Roofing" in pitches["approaches"]["local_contrast"]["text"]

    # Rating 5.0 and 75 reviews triggers local_contrast
    assert pitches["recommended"] == "local_contrast"
    assert pitches["recommended_slot"] == "slot_3"
    assert pitches["primary_wa_url"] is not None


def test_bare_roofing_pitches():
    bare_lead = {
        "id": 97,
        "business_name": "Bare Roofing Dallas",
        "city": "Dallas",
        "niche": "commercial roofer",
        "rating": 4.9,
        "review_count": 14,
        "clean_phone": "+1 214-432-4464",
        "website_url": "https://www.bareroofingtx.com/",
        "audit": {
            "flaw_codes": ["server_delay"],
            "ttfb_ms": 3200
        }
    }
    pitches = generate_pitches(bare_lead)

    # Reviews < 30 (14 reviews) -> defaults to naive_curiosity (slot 1)
    assert pitches["recommended"] == "naive_curiosity"
    assert pitches["recommended_slot"] == "slot_1"

    # Preserves clean name without alteration
    for k, app in pitches["approaches"].items():
        assert "Bare Roofing Dallas" in app["text"]
        assert "—" not in app["text"]
        assert "http" not in app["text"]
        assert app["word_count"] <= 65

    # Mentions ~3 seconds server latency and starts directly with problem
    assert pitches["approaches"]["naive_curiosity"]["text"].startswith("Is it supposed to")
    assert "~3 seconds" in pitches["approaches"]["naive_curiosity"]["text"]
    assert "15-second screen recording" in pitches["approaches"]["loss_aversion"]["text"]

    # Business effect for server_delay
    effect = "People assume the site is down and tap the next listing."
    assert effect in pitches["approaches"]["loss_aversion"]["text"]
    assert effect in pitches["approaches"]["local_contrast"]["text"]
    assert effect in pitches["approaches"]["reputation_dissonance"]["text"]

    # Reviews < 30 drops competitive claim
    assert "beat half" not in pitches["approaches"]["local_contrast"]["text"]
    assert "Spotted this checking Bare Roofing Dallas." in pitches["approaches"]["local_contrast"]["text"]

    # City duplication avoided: 'Dallas' is inside business name so niche has no redundant 'Dallas'
    assert "Dallas commercial roofing" not in pitches["approaches"]["naive_curiosity"]["text"]
    assert "commercial roofing listings" in pitches["approaches"]["naive_curiosity"]["text"]

    # Router text cleaned of AI tell
    assert "the site stalls for a few seconds before loading" in pitches["approaches"]["gatekeeper"]["text"].lower()


def test_apex_family_dental_pitches():
    apex_lead = {
        "id": 105,
        "business_name": "Apex Family Dental",
        "city": "Austin",
        "niche": "dentist",
        "rating": 4.9,
        "review_count": 112,
        "clean_phone": "+1 512-555-4321",
        "website_url": "https://apexfamilydental.com",
        "audit": {
            "flaw_codes": ["unclickable_phone"]
        }
    }
    pitches = generate_pitches(apex_lead)

    assert pitches["recommended"] == "local_contrast"
    assert pitches["recommended_slot"] == "slot_3"

    effect = "Nobody memorizes a 10-digit number on a phone, so they tap the next listing that dials."
    assert effect in pitches["approaches"]["loss_aversion"]["text"]
    assert effect in pitches["approaches"]["local_contrast"]["text"]
    assert effect in pitches["approaches"]["reputation_dissonance"]["text"]

    # Austin dentist listings (no roofing assumptions)
    assert "Austin dentist listings" in pitches["approaches"]["naive_curiosity"]["text"]
    assert "even though your reviews beat half the Austin listings next to you." in pitches["approaches"]["local_contrast"]["text"]
    assert "quick screenshot" in pitches["approaches"]["loss_aversion"]["text"]


def test_carter_injury_law_pitches():
    carter_lead = {
        "id": 106,
        "business_name": "Carter Injury Law",
        "city": "Houston",
        "niche": "personal injury lawyer",
        "rating": 4.8,
        "review_count": 22,
        "clean_phone": "+1 713-555-7890",
        "website_url": "https://carterinjurylaw.com",
        "audit": {
            "flaw_codes": ["missing_lead_capture"]
        }
    }
    pitches = generate_pitches(carter_lead)

    # 22 reviews (< 30) -> defaults to naive_curiosity
    assert pitches["recommended"] == "naive_curiosity"
    assert pitches["recommended_slot"] == "slot_1"

    effect = "Anyone browsing after hours has nowhere to leave their info, so those inquiries disappear."
    assert effect in pitches["approaches"]["loss_aversion"]["text"]
    assert effect in pitches["approaches"]["local_contrast"]["text"]
    assert effect in pitches["approaches"]["reputation_dissonance"]["text"]

    # Houston personal injury lawyer listings
    assert "Houston personal injury lawyer listings" in pitches["approaches"]["naive_curiosity"]["text"]
    assert "Spotted this checking Carter Injury Law." in pitches["approaches"]["local_contrast"]["text"]
    assert "beat half" not in pitches["approaches"]["local_contrast"]["text"]


def test_preview_window_rules():
    banned_starters = ["hey", "hi", "hello", "quick question", "quick weird", "you probably"]
    banned_words = ["homeowner", "quote", "web guy", "bounce rate", "potential", "just to open", "just checked"]

    test_leads = [
        # 1. Measured flaw lead
        {
            "id": 94,
            "business_name": "FOX Roofing | Dallas",
            "city": "Dallas",
            "niche": "commercial roofer",
            "rating": 5.0,
            "review_count": 75,
            "clean_phone": "+1 214-505-3374",
            "website_url": "https://foxroofing.co/",
            "audit": {"flaw_codes": ["slow_load"], "load_time_sec": 9.6}
        },
        # 2. Server delay lead (3200ms fixture)
        {
            "id": 97,
            "business_name": "Bare Roofing Dallas",
            "city": "Dallas",
            "niche": "commercial roofer",
            "rating": 4.9,
            "review_count": 14,
            "clean_phone": "+1 214-432-4464",
            "website_url": "https://www.bareroofingtx.com/",
            "audit": {"flaw_codes": ["server_delay"], "ttfb_ms": 3200}
        },
        # 3. Dental niche
        {
            "id": 105,
            "business_name": "Apex Family Dental",
            "city": "Austin",
            "niche": "dentist",
            "rating": 4.9,
            "review_count": 112,
            "clean_phone": "+1 512-555-4321",
            "website_url": "https://apexfamilydental.com",
            "audit": {"flaw_codes": ["unclickable_phone"]}
        },
        # 4. Law niche
        {
            "id": 106,
            "business_name": "Carter Injury Law",
            "city": "Houston",
            "niche": "personal injury lawyer",
            "rating": 4.8,
            "review_count": 22,
            "clean_phone": "+1 713-555-7890",
            "website_url": "https://carterinjurylaw.com",
            "audit": {"flaw_codes": ["missing_lead_capture"]}
        },
        # 5. Dead domain lead
        {
            "id": 102,
            "business_name": "Dead Domain Roofing LLC",
            "city": "Dallas",
            "clean_phone": "+1 214-555-1234",
            "website_url": "https://deaddomainroofing.com",
            "audit": {"flaw_codes": ["dead_domain"]}
        },
        # 6. No website lead
        {
            "id": 103,
            "business_name": "Lone Star Commercial Roofing LLC",
            "city": "Dallas",
            "clean_phone": "+1 214-555-9876",
            "website_url": None,
            "audit": {"flaw_codes": ["no_website"]}
        }
    ]

    for lead in test_leads:
        pitches = generate_pitches(lead)
        clean_name = clean_contractor_name(lead["business_name"])

        for approach_key, app in pitches["approaches"].items():
            text = app["text"]
            assert text, f"Empty text for {lead['business_name']} {approach_key}"

            # 1. Zero greetings or filler openers
            low = text.lower()
            for b in banned_starters:
                assert not low.startswith(b), f"Pitch {approach_key} starts with banned '{b}': {text[:40]}"

            # 2. Never start with business name
            assert not text.startswith(clean_name), f"Pitch {approach_key} starts with business name: {text[:40]}"

            # 3. Zero parenthetical merge-tag formatting
            assert f"({clean_name})" not in text, f"Parenthetical biz name found in {approach_key}: {text}"
            assert f"({lead['business_name']})" not in text, f"Parenthetical raw biz name in {approach_key}: {text}"

            # 4. Zero banned/roofing-specific words or softeners
            for bw in banned_words:
                assert bw not in low, f"Found banned word '{bw}' in {lead['business_name']} {approach_key}: {text}"

            # 5. Zero em-dashes
            assert "—" not in text, f"Pitch {approach_key} has em-dash: {text}"

            # 6. Zero links
            assert "http" not in text and "www." not in text, f"Pitch {approach_key} has link: {text}"

            # 7. Zero duplicate 'on mobile' / 'on my phone'
            mobile_phrases = low.count("on mobile") + low.count("on my phone")
            assert mobile_phrases <= 1, f"Pitch {approach_key} has duplicate mobile phrases ({mobile_phrases}): {text}"

            # 8. Preview window (first 45 chars) has meaningful signal
            preview_45 = text[:45]
            assert len(preview_45) <= 45


def test_healthy_zero_flaw_lead():
    healthy_lead = {
        "id": 98,
        "business_name": "Superior One Roofing & Construction, Inc.",
        "city": "Dallas",
        "rating": 5.0,
        "review_count": 284,
        "clean_phone": "+1 972-893-3374",
        "website_url": "https://s1roofing.com/",
        "audit": {
            "flaw_codes": [],
            "flaw_labels": []
        }
    }
    pitches = generate_pitches(healthy_lead)
    assert pitches["needs_manual_note"] is True
    assert pitches["specific_flaw"] == ""
    for k, app in pitches["approaches"].items():
        assert app["text"] == ""
        assert app["word_count"] == 0
        assert app["wa_url"] is None


def test_form_only_recommendation():
    form_lead = {
        "id": 101,
        "business_name": "Commercial Peak Roofing",
        "city": "Dallas",
        "form_url": "https://commercialpeakroofing.com/contact",
        "website_url": "https://commercialpeakroofing.com",
        "audit": {
            "flaw_codes": ["broken_mobile"]
        }
    }
    pitches = generate_pitches(form_lead)
    assert pitches["recommended"] == "gatekeeper"
    assert pitches["recommended_slot"] == "slot_4"


def test_dead_domain_recommendation():
    dead_lead = {
        "id": 102,
        "business_name": "Dead Domain Roofing LLC",
        "city": "Dallas",
        "clean_phone": "+1 214-555-1234",
        "website_url": "https://deaddomainroofing.com",
        "audit": {
            "flaw_codes": ["dead_domain"]
        }
    }
    pitches = generate_pitches(dead_lead)
    assert pitches["recommended"] == "accusation_audit"
    assert pitches["recommended_slot"] == "slot_2"
    assert clean_contractor_name(dead_lead["business_name"]) == "Dead Domain Roofing"


def test_b12_clean_city_name():
    assert clean_city_name("Dallas TX") == "Dallas"
    assert clean_city_name("Austin, TX") == "Austin"
    assert clean_city_name("San Antonio TX") == "San Antonio"
    assert clean_city_name("Miami, FL") == "Miami"
    assert clean_city_name("New York, NY") == "New York"
    assert clean_city_name("") == ""
    assert clean_city_name(None) == ""
    assert clean_city_name("   ") == ""
    assert clean_city_name("None") == ""


def test_b12_clean_contractor_name_anchored():
    # Preserves mid-name tokens like "Co" or "Inc"
    assert clean_contractor_name("Apex Co Roofing") == "Apex Co Roofing"
    assert clean_contractor_name("Premier Co Builders") == "Premier Co Builders"
    # Strips trailing and chained legal suffixes
    assert clean_contractor_name("Smith Roofing Co., L.L.C.") == "Smith Roofing"
    assert clean_contractor_name("Smith Roofing Co., LLC") == "Smith Roofing"
    assert clean_contractor_name("Superior One Roofing & Construction, Inc.") == "Superior One Roofing & Construction"
    assert clean_contractor_name("Lone Star Roofing, Ltd.") == "Lone Star Roofing"


def test_b12_empty_city_no_dallas_invention():
    # Lead with city="" or city=None never outputs "Dallas" in any of the 8 pitches
    lead_no_city = {
        "id": 201,
        "business_name": "Lone Star Shingle",
        "city": "",
        "niche": "roofing",
        "clean_phone": "+1 512-555-0100",
        "website_url": "https://lonestarshingle.com",
        "audit": {"flaw_codes": ["slow_load"], "load_time_sec": 8.0}
    }
    pitches = generate_pitches(lead_no_city)
    for k, app in pitches["approaches"].items():
        assert "Dallas" not in app["text"], f"Found 'Dallas' in pitch {k} for lead with empty city"

    lead_none_city = {
        "id": 202,
        "business_name": "Apex Commercial",
        "city": None,
        "niche": "commercial roofing",
        "clean_phone": "+1 512-555-0100",
        "website_url": None,
        "audit": {"flaw_codes": ["no_website"]}
    }
    pitches_none = generate_pitches(lead_none_city)
    for k, app in pitches_none["approaches"].items():
        assert "Dallas" not in app["text"], f"Found 'Dallas' in pitch {k} for lead with None city"


def test_b12_bare_roofing_dallas_city_tx_no_duplication():
    # "Bare Roofing Dallas" with city="Dallas TX" never repeats "Dallas" and never prints "Dallas TX listings"
    bare_tx_lead = {
        "id": 203,
        "business_name": "Bare Roofing Dallas",
        "city": "Dallas TX",
        "niche": "commercial roofer",
        "rating": 4.9,
        "review_count": 14,
        "clean_phone": "+1 214-432-4464",
        "website_url": "https://www.bareroofingtx.com/",
        "audit": {"flaw_codes": ["server_delay"], "ttfb_ms": 3200}
    }
    pitches = generate_pitches(bare_tx_lead)
    for k, app in pitches["approaches"].items():
        assert "Dallas TX" not in app["text"], f"Found 'Dallas TX' in pitch {k}"
        assert "Dallas commercial roofing" not in app["text"], f"Found duplicate 'Dallas' in {k}"
        assert "Dallas listings" not in app["text"], f"Found 'Dallas listings' in {k}"
    assert "commercial roofing listings" in pitches["approaches"]["naive_curiosity"]["text"]


def test_b12_no_ai_weakeners_or_repetition_in_pitches():
    # Zero occurrences of "actually", "really", "Mobile Slag", "on cellular data. People on cellular data",
    # or "on Google. Most people hit back after 3 seconds and call the next listing on Google"
    sample_leads = [
        {
            "business_name": "Apex Phone Test",
            "city": "Plano",
            "audit": {"flaw_codes": ["unclickable_phone"]},
            "website_url": "https://apexphone.com"
        },
        {
            "business_name": "Slow Site Test",
            "city": "Frisco",
            "audit": {"flaw_codes": ["slow_load"]},
            "website_url": "https://slowsite.com"
        },
        {
            "business_name": "Heavy Site Test",
            "city": "Dallas",
            "audit": {"flaw_codes": ["heavy_page"], "page_weight_mb": 6.2},
            "website_url": "https://heavysite.com"
        },
        {
            "business_name": "Server Delay Test",
            "city": "Austin",
            "audit": {"flaw_codes": ["server_delay"], "ttfb_ms": 2500},
            "website_url": "https://delayedsite.com"
        }
    ]
    for lead in sample_leads:
        pitches = generate_pitches(lead)
        # Check psychology meta
        for k, app in pitches["approaches"].items():
            assert "Mobile Slag" not in app.get("psychology", "")
            low = app["text"].lower()
            assert "actually" not in low, f"Found 'actually' in {lead['business_name']} {k}: {app['text']}"
            assert "really slowly" not in low, f"Found 'really slowly' in {lead['business_name']} {k}: {app['text']}"
            assert "on cellular data. people on cellular data" not in low
            assert "on google. most people hit back after 3 seconds and call the next listing on google" not in low
            assert "on google. people assume the site is down and tap the next listing on google" not in low


def test_b12_toll_free_smart_recommendation():
    # A lead with clean_phone="+18005551234", line_type="Toll-Free", and form_url="https://example.com/contact" recommends "gatekeeper" ("slot_4")
    lead_toll_free = {
        "business_name": "Toll Free National Roofing",
        "clean_phone": "+1 800-555-1234",
        "line_type": "Toll-Free",
        "form_url": "https://example.com/contact",
        "website_url": "https://example.com",
        "audit": {"flaw_codes": ["broken_mobile"]}
    }
    pitches = generate_pitches(lead_toll_free)
    assert pitches["recommended"] == "gatekeeper"
    assert pitches["recommended_slot"] == "slot_4"

    # Also test area code 888 without explicit line_type string
    lead_888 = {
        "business_name": "888 National Roofing",
        "clean_phone": "+1 888-555-4321",
        "generic_email": "info@888roofing.com",
        "website_url": "https://888roofing.com",
        "audit": {"flaw_codes": ["slow_load"]}
    }
    pitches_888 = generate_pitches(lead_888)
    assert pitches_888["recommended"] == "gatekeeper"
    assert pitches_888["recommended_slot"] == "slot_4"


def test_b27_audit_json_auto_resolve_and_bot_challenge_manual_note():
    import json
    # Raw database lead with audit_json as a JSON string and no unpacked 'audit' key
    raw_lead = {
        "id": 99,
        "business_name": "Apex Roofing",
        "city": "Plano",
        "niche": "commercial roofer",
        "rating": 4.9,
        "review_count": 50,
        "clean_phone": "+1 214-555-9999",
        "website_url": "https://apexroofing.com",
        "audit_json": json.dumps({
            "flaw_codes": ["slow_load"],
            "flaw_labels": ["Slow Load (7.20s)"],
            "load_time_sec": 7.2
        })
    }
    pitches = generate_pitches(raw_lead)
    assert pitches["needs_manual_note"] is False
    assert "Apex Roofing" in pitches["approaches"]["naive_curiosity"]["text"]
    assert pitches["approaches"]["naive_curiosity"]["word_count"] > 0
    assert pitches["approaches"]["naive_curiosity"]["word_count"] <= 45

    # Bot-challenge-gated leads must NOT claim visitors hit a security screen; require manual verification
    bot_lead = {
        "id": 100,
        "business_name": "Apex Roofing",
        "city": "Plano",
        "niche": "commercial roofer",
        "rating": 4.9,
        "review_count": 50,
        "clean_phone": "+1 214-555-9999",
        "website_url": "https://apexroofing.com",
        "audit_json": json.dumps({
            "flaw_codes": ["bot_challenge_gated"],
            "flaw_labels": ["Protected by Host Security / Bot Gate (Manual Verification Required)"]
        })
    }
    bot_pitches = generate_pitches(bot_lead)
    assert bot_pitches["needs_manual_note"] is True
    for k, app in bot_pitches["approaches"].items():
        assert app["text"] == "", f"Expected empty pitch for bot_challenge_gated approach {k}"


if __name__ == "__main__":
    test_clean_contractor_name()
    test_get_proof_asset()
    test_get_flaw_variants_coverage()
    test_rounded_metrics()
    test_fox_roofing_pitches()
    test_bare_roofing_pitches()
    test_healthy_zero_flaw_lead()
    test_form_only_recommendation()
    test_dead_domain_recommendation()
    test_apex_family_dental_pitches()
    test_carter_injury_law_pitches()
    test_preview_window_rules()
    test_b12_clean_city_name()
    test_b12_clean_contractor_name_anchored()
    test_b12_empty_city_no_dallas_invention()
    test_b12_bare_roofing_dallas_city_tx_no_duplication()
    test_b12_no_ai_weakeners_or_repetition_in_pitches()
    test_b12_toll_free_smart_recommendation()
    print("\nALL TEST CASES PASSED SUCCESSFULLY!")
