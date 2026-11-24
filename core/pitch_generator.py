import json
import re
import urllib.parse
from typing import Dict, Any, Optional, List, Tuple

# Technical flaw priority ordering: highest customer-conversion impact first
PRIORITY_ORDER = [
    "broken_mobile",             # 1. Page breaks sideways on phone
    "unclickable_phone",          # 2. Cannot tap phone number to call
    "missing_lead_capture",       # 3. No quote form or CTA button
    "obstructive_sticky_header",  # 4. Giant sticky bar covers viewport
    "slow_load",                  # 5. Wall-clock load duration threshold exceeded
    "server_delay",               # 6. High TTFB / initial response latency
    "high_ttfb",
    "heavy_page",                 # 7. Multi-megabyte payload dragging on mobile
    "builder_bloat",              # 8. Excessive DOM nodes
    "zero_visual_proof",          # 9. No project photos
    "zero_trust_anchors",         # 10. No license/insurance/reviews listed
    "thin_architecture",          # 11. Low content / DOM
    "content_starved"
]

# Regex patterns for matching free-form flaw labels or status reasons
FLAW_LABEL_PATTERNS: List[Tuple[str, str]] = [
    (r"broken\s*mobile|horizontal\s*overflow", "broken_mobile"),
    (r"unclickable\s*phone|tap-to-call", "unclickable_phone"),
    (r"missing\s*lead\s*capture|no\s*quote\s*form", "missing_lead_capture"),
    (r"obstructive\s*sticky\s*header|sticky\s*header", "obstructive_sticky_header"),
    (r"slow\s*load", "slow_load"),
    (r"server\s*delay|high\s*ttfb", "server_delay"),
    (r"heavy\s*page", "heavy_page"),
    (r"builder\s*bloat", "builder_bloat"),
    (r"visual\s*proof|no\s*job\s*photos", "zero_visual_proof"),
    (r"trust\s*anchors|no\s*license|no\s*insurance", "zero_trust_anchors"),
    (r"content\s*starved|thin\s*architecture", "thin_architecture"),
]


def clean_city_name(raw_city: Optional[str]) -> str:
    """
    Cleans city string:
    - Returns "" if empty/None (never falls back to a hardcoded city).
    - Strips trailing 2-letter US state abbreviations (e.g. 'Dallas TX' -> 'Dallas', 'Austin, TX' -> 'Austin').
    """
    if not raw_city:
        return ""
    c = str(raw_city).strip()
    if not c or c.lower() in {"none", "null", "undefined", "n/a"}:
        return ""
    state_pattern = re.compile(
        r",?\s+\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\.?\s*$",
        re.IGNORECASE
    )
    c = state_pattern.sub("", c).strip()
    c = re.sub(r"[,;:\-–—]+$", "", c).strip()
    return c


def clean_contractor_name(raw_name: Optional[str]) -> str:
    """
    Cleans contractor business names for natural, conversational outreach:
    1. Splits on location / tagline separators ('|', '–', '—', or ' - ') and keeps the primary brand name.
    2. Strips legal suffixes (LLC, Inc, Corp, Co, Ltd) strictly at end of string in a loop.
    3. Preserves clean geographical brand names without separators (e.g. 'Bare Roofing Dallas').
    """
    if not raw_name:
        return "there"
    name = raw_name.strip()

    # 1. Split on location / tagline separators: '|', '–', '—', or ' - '
    parts = re.split(r"\s*[|–—]\s*|\s+-\s+", name)
    if parts and parts[0].strip():
        name = parts[0].strip()

    # 2. Strip legal suffixes anchored to end of string in a loop: LLC, Inc, Corp, Co, Ltd
    suffix_pat = re.compile(r",?\s+\b(?:L\.L\.C\.?|LLC|Inc\.?|Corp\.?|Co\.?|Ltd\.?)\.?\s*$", re.IGNORECASE)
    while True:
        updated = suffix_pat.sub("", name).strip()
        if updated == name:
            break
        name = updated

    # 3. Clean any trailing punctuation left over
    name = re.sub(r"[,;:\-–—]+$", "", name).strip()
    return name or raw_name


def _cap(s: str) -> str:
    """Capitalizes the first character only, preserving existing acronyms/casing (e.g. CTA, MB, TTFB)."""
    if not s:
        return ""
    return s[0].upper() + s[1:]


def get_proof_asset(flaw_code: Optional[str]) -> str:
    """Matches appropriate proof asset type based on technical vs visual flaw nature."""
    if flaw_code in {"slow_load", "server_delay", "high_ttfb", "heavy_page", "builder_bloat"}:
        return "15-second screen recording"
    return "quick screenshot"


def get_flaw_variants(flaw_code: Optional[str], audit: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    Returns 7 distinct, grammatically tailored sentence versions for a technical flaw code:
      - curiosity: used in natural questioning templates ('Supposed to [curiosity]...')
      - direct: used in direct statements ('[direct] on mobile')
      - impact: used in revenue leak descriptions ('Leads are hitting [impact]')
      - router: used in technical gatekeeper routing ('[router]')
      - contrast: used in local reputation contrast ('Only snag is [contrast]')
      - preview_hook: capitalized ultra-tight problem phrase (<= 40 chars) for notification window
      - business_effect: plain-English, factual 1-level-deeper business consequence

    Never repeats 'on my phone' or 'on mobile' inside the variant text itself.
    Uses real measured telemetry (load_time_sec, page_weight_mb, ttfb_ms) where available.
    Niche-agnostic (works for contractors, dental, medical, law, HVAC, local retail).
    """
    audit = audit or {}
    load_sec = audit.get("load_time_sec")
    sec_int = round(load_sec) if (isinstance(load_sec, (int, float)) and load_sec > 0) else None

    ttfb_ms = audit.get("ttfb_ms")
    ttfb_sec = max(2, round(ttfb_ms / 1000.0)) if (isinstance(ttfb_ms, (int, float)) and ttfb_ms > 0) else None

    weight_mb = audit.get("page_weight_mb")
    mb_int = round(weight_mb) if (isinstance(weight_mb, (int, float)) and weight_mb > 0) else None

    code = (flaw_code or "").strip().lower()

    if code == "slow_load":
        if sec_int is not None:
            return {
                "curiosity": f"take ~{sec_int} seconds to load",
                "direct": f"it took ~{sec_int} seconds to open",
                "impact": f"a ~{sec_int}-second blank wait",
                "router": f"mobile load time is hitting ~{sec_int}s",
                "contrast": f"the site takes ~{sec_int} seconds to open while people are comparing local options",
                "preview_hook": f"Site took ~{sec_int}s to load on mobile",
                "business_effect": "Most people hit back after 3 seconds and call the next listing."
            }
        return {
            "curiosity": "take that long to load",
            "direct": "it took a long time to open",
            "impact": "a long blank wait",
            "router": "mobile site is loading slowly",
            "contrast": "the site drags while people are comparing local options",
            "preview_hook": "Site took forever to load on mobile",
            "business_effect": "Most people hit back after 3 seconds and call the next listing."
        }

    elif code in ("server_delay", "high_ttfb"):
        if ttfb_sec is not None:
            return {
                "curiosity": f"hang for ~{ttfb_sec} seconds before loading",
                "direct": f"the page stalled for ~{ttfb_sec} seconds before anything showed up",
                "impact": f"a ~{ttfb_sec}-second dead pause before anything even starts loading",
                "router": "the site stalls for a few seconds before loading",
                "contrast": f"the site stalls for ~{ttfb_sec} seconds before loading when people tap your listing",
                "preview_hook": f"Site hung ~{ttfb_sec}s before loading",
                "business_effect": "People assume the site is down and tap the next listing."
            }
        return {
            "curiosity": "hang for a few seconds before loading",
            "direct": "the page stalled for a few seconds before anything showed up",
            "impact": "a dead pause before the site even starts loading",
            "router": "the site stalls for a few seconds before loading",
            "contrast": "the site stalls for a few seconds before loading when people tap your listing",
            "preview_hook": "Site stalled before loading on mobile",
            "business_effect": "People assume the site is down and tap the next listing."
        }

    elif code == "broken_mobile":
        return {
            "curiosity": "cut off sideways on a phone screen",
            "direct": "the layout cuts off sideways",
            "impact": "a cut-off mobile layout",
            "router": "mobile layout has horizontal overflow",
            "contrast": "the layout cuts off sideways when people open it on a phone",
            "preview_hook": "Mobile layout cuts off sideways",
            "business_effect": "First-time visitors assume the business is outdated and move to a competitor."
        }

    elif code == "unclickable_phone":
        return {
            "curiosity": "have a phone number you can't tap to call",
            "direct": "tapping the phone number doesn't dial",
            "impact": "an unclickable phone number",
            "router": "phone number isn't set up as tap-to-call",
            "contrast": "people can't tap your number to call straight from Google",
            "preview_hook": "Phone number isn't clickable on mobile",
            "business_effect": "Nobody memorizes a 10-digit number on a phone, so they tap the next listing that dials."
        }

    elif code == "missing_lead_capture":
        return {
            "curiosity": "have no contact or booking button showing",
            "direct": "there's no obvious button or form to get in touch",
            "impact": "a homepage with no clear contact button",
            "router": "no contact form or CTA showing on mobile",
            "contrast": "there's no easy way to reach out when people tap your listing",
            "preview_hook": "No contact button showing on mobile",
            "business_effect": "Anyone browsing after hours has nowhere to leave their info, so those inquiries disappear."
        }

    elif code == "obstructive_sticky_header":
        return {
            "curiosity": "have the top bar cover half the screen",
            "direct": "the top header locks over a big chunk of the screen",
            "impact": "a sticky header blocking most of the screen",
            "router": "sticky header is blocking the mobile viewport",
            "contrast": "the top bar covers way too much of the screen on a phone",
            "preview_hook": "Top bar covers half the mobile screen",
            "business_effect": "Visitors can barely read what you offer on a phone and close the tab."
        }

    elif code == "heavy_page":
        if mb_int is not None:
            return {
                "curiosity": f"pull ~{mb_int}MB and stall on cellular",
                "direct": f"the homepage loads ~{mb_int}MB and drags hard",
                "impact": f"a ~{mb_int}MB page that crawls on cellular",
                "router": f"mobile page weight is around {mb_int}MB",
                "contrast": f"the mobile page weighs ~{mb_int}MB and drags on mobile",
                "preview_hook": f"Homepage is ~{mb_int}MB on mobile",
                "business_effect": "People on cellular data give up waiting and pick the next listing."
            }
        return {
            "curiosity": "pull huge files and stall on cellular",
            "direct": "the homepage is super heavy and drags hard",
            "impact": "a bloated page that crawls on cellular",
            "router": "mobile page weight is unusually heavy",
            "contrast": "the mobile page is heavy and drags on mobile",
            "preview_hook": "Homepage is way too heavy on mobile",
            "business_effect": "People on cellular data give up waiting and pick the next listing."
        }

    elif code == "builder_bloat":
        return {
            "curiosity": "lag heavily when scrolling",
            "direct": "scrolling feels super sluggish",
            "impact": "a laggy mobile page",
            "router": "heavy scripts are causing scroll lag on mobile",
            "contrast": "the site lags when visitors try to scroll through your services",
            "preview_hook": "Mobile site lags hard when scrolling",
            "business_effect": "Visitors get frustrated scrolling on mobile and back out to Google."
        }

    elif code == "zero_visual_proof":
        return {
            "curiosity": "hide real photos of your work",
            "direct": "no real photos of your work show up",
            "impact": "a site with no visible photos of your work",
            "router": "photos of your work aren't displaying on mobile",
            "contrast": "no real photos of your work show up when people check you out",
            "preview_hook": "No real work photos on mobile",
            "business_effect": "New customers see no real photos to trust before calling, so they compare other listings."
        }

    elif code == "zero_trust_anchors":
        return {
            "curiosity": "leave out your reviews and credentials on the homepage",
            "direct": "there's no mention of reviews or credentials up front",
            "impact": "a homepage missing basic trust signals like reviews or credentials",
            "router": "reviews and credentials aren't showing on the homepage",
            "contrast": "none of your reviews or credentials show up on the homepage",
            "preview_hook": "No reviews or credentials on homepage",
            "business_effect": "People comparing listings see no reviews or credentials up front and pick someone safer."
        }

    elif code in ("thin_architecture", "content_starved"):
        return {
            "curiosity": "barely show your services",
            "direct": "the homepage barely explains what services you offer",
            "impact": "a bare-bones page that barely lists your services",
            "router": "main service details aren't loading on the mobile homepage",
            "contrast": "the mobile homepage barely tells visitors what you handle",
            "preview_hook": "Services barely show on the mobile site",
            "business_effect": "Visitors can't tell if you handle what they need, so they check the next listing."
        }

    return {
        "curiosity": "render with conversion bottlenecks",
        "direct": "a few conversion issues show up on mobile",
        "impact": "a sluggish mobile page",
        "router": "there are mobile performance issues",
        "contrast": "the mobile site has conversion issues when people check you out",
        "preview_hook": "Mobile site has conversion bottlenecks",
        "business_effect": "Visitors running into issues on mobile usually back out to Google and call the next listing."
    }


def _resolve_lead_audit(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Resolves unpacked audit dictionary from lead['audit'] or lead['audit_json']."""
    audit = lead.get("audit")
    if audit and isinstance(audit, dict):
        return audit
    audit_raw = lead.get("audit_json")
    if audit_raw and isinstance(audit_raw, str):
        try:
            return json.loads(audit_raw)
        except Exception:
            return {}
    elif isinstance(audit_raw, dict):
        return audit_raw
    return {}


def get_highest_priority_flaw(lead: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    """
    Identifies the single highest-priority verified flaw for a live website
    and formats its 5 tailored sentence variants.
    Returns: (chosen_flaw_code, variants_dict) or (None, None) if 0 verified flaws exist.
    """
    audit = _resolve_lead_audit(lead)
    flaw_codes = [str(c).strip().lower() for c in (audit.get("flaw_codes") or [])]
    flaw_labels = [str(l).strip().lower() for l in (audit.get("flaw_labels") or [])]
    status_reason = str(lead.get("status_reason") or "").lower()

    detected = set()

    for c in flaw_codes:
        if c in PRIORITY_ORDER:
            detected.add(c)

    for label in flaw_labels:
        for pat, canon_key in FLAW_LABEL_PATTERNS:
            if re.search(pat, label):
                detected.add(canon_key)

    for pat, canon_key in FLAW_LABEL_PATTERNS:
        if re.search(pat, status_reason):
            detected.add(canon_key)

    chosen_key = None
    for k in PRIORITY_ORDER:
        if k in detected:
            chosen_key = k
            break

    if not chosen_key:
        return None, None

    variants = get_flaw_variants(chosen_key, audit)
    return chosen_key, variants


def translate_flaw(lead: Dict[str, Any]) -> str:
    """
    Translates the single highest-priority technical flaw into plain-English contractor terminology.
    Returns the 'direct' variant or '' if no verified flaw exists.
    """
    _, variants = get_highest_priority_flaw(lead)
    if variants:
        return variants.get("direct", "")
    return ""


def extract_phone_digits(lead: Dict[str, Any]) -> Optional[str]:
    """Sanitizes phone number for WhatsApp wa.me links."""
    phone = lead.get("clean_phone") or lead.get("phone")
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = f"1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        pass
    return digits if len(digits) >= 10 else None


def generate_pitches(lead: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generates all 8 psychological outreach pitches organized into 4 swappable slots.
    Computed on-demand without SQLite storage.
    Channel-neutral wording (works on WhatsApp, Email, Contact Forms).
    Zero em-dashes, zero links.
    """
    biz_name = clean_contractor_name(lead.get("business_name"))
    city = clean_city_name(lead.get("city"))

    # Niche-agnostic category handling
    raw_niche = (lead.get("niche") or lead.get("category") or "").strip().lower()
    if raw_niche == "commercial roofer":
        niche = "commercial roofing"
    elif raw_niche == "roofer":
        niche = "roofing"
    else:
        niche = raw_niche

    if not city or city.lower() in biz_name.lower():
        city_prefix = ""
        city_or_local = "local"
        loc_str = ""
        city_niche = f"local {niche} listings" if niche else "local listings"
    else:
        city_prefix = f"{city} "
        city_or_local = city
        loc_str = f" in {city}"
        city_niche = f"{city_prefix}{niche} listings" if niche else f"{city} listings"

    rating_val = lead.get("rating")
    rating_str = f"{rating_val:.1f}" if (isinstance(rating_val, (int, float)) and rating_val > 0) else "5.0"
    reviews_count = lead.get("review_count") or 0

    has_website_url = bool(lead.get("website_url"))
    audit = _resolve_lead_audit(lead)
    flaw_codes = [str(c).strip().lower() for c in (audit.get("flaw_codes") or [])]
    flaw_labels = [str(l).strip().lower() for l in (audit.get("flaw_labels") or [])]
    status_reason = str(lead.get("status_reason") or "").lower()

    # Determine site category branch
    is_dead_domain = (
        bool(audit.get("is_unreachable")) or
        "dead_domain" in flaw_codes or
        "dead domain" in status_reason or
        any("dead domain" in l for l in flaw_labels) or
        "dns failure" in status_reason or
        "inaccessible" in status_reason
    )

    is_no_website = (
        not is_dead_domain and (
            "no_website" in flaw_codes or
            "no website" in status_reason or
            any("no website" in l for l in flaw_labels) or
            not has_website_url
        )
    )

    chosen_flaw_key, variants = get_highest_priority_flaw(lead)
    needs_manual_note = False
    specific_flaw = ""
    proof_asset = get_proof_asset(chosen_flaw_key)

    scripts: Dict[str, str] = {}

    if is_no_website:
        proof_asset = "quick idea"
        specific_flaw = "no website linked on Google Maps"

        # Slot 1
        scripts["naive_curiosity"] = (
            f"Staying off a website on purpose?\n\n"
            f"Saw {biz_name} on Google Maps{loc_str} with no site linked and wondered if you're strictly referral-only."
        )
        scripts["loss_aversion"] = (
            f"Missing website link on Google Maps. People checking Google Maps for {biz_name} skip straight to listings with a site they can look over.\n\n"
            f"Putting a simple page up soon, or staying referral-only?"
        )

        # Slot 2
        scripts["accusation_audit"] = (
            f"No website linked on Google Maps when checking {biz_name}. Probably get pitched sites every week, so feel free to ignore this.\n\n"
            f"Staying referral-only on purpose, or open to seeing a rough 1-page idea?"
        )
        scripts["no_oriented"] = (
            f"No website linked on Google Maps for {biz_name} yet.\n\n"
            f"Would you be against me sending a quick outline of what a simple inquiry page could look like?"
        )

        # Slot 3
        if reviews_count >= 30:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but no website linked on Google. People checking Google Maps skip straight to listings with a site they can look over. Spotted this on {biz_name}, even though your reviews beat half the {city_or_local} listings next to you.\n\n"
                f"Staying referral-only on purpose, or thinking about putting a simple page up?"
            )
        elif reviews_count > 0:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but no website linked on Google. People checking Google Maps skip straight to listings with a site they can look over. Spotted this checking {biz_name}.\n\n"
                f"Staying referral-only on purpose, or thinking about putting a simple page up?"
            )
        else:
            scripts["local_contrast"] = (
                f"No website linked on Google Maps compared to other {city_niche}. People checking Google Maps skip straight to listings with a site they can look over. Spotted this checking {biz_name}.\n\n"
                f"Staying referral-only on purpose, or thinking about putting a simple page up?"
            )

        if reviews_count > 0:
            scripts["reputation_dissonance"] = (
                f"No website linked on Google, surprised given {rating_str}★ across {reviews_count} reviews for {biz_name}. People checking Google Maps skip straight to listings with a site they can look over.\n\n"
                f"Keeping things referral-only, or mind if I send a quick inquiry page idea?"
            )
        else:
            scripts["reputation_dissonance"] = (
                f"No website linked on Google Maps for {biz_name}{loc_str}. People checking Google Maps skip straight to listings with a site they can look over.\n\n"
                f"Keeping things referral-only, or mind if I send a quick inquiry page idea?"
            )

        # Slot 4
        scripts["gatekeeper"] = (
            f"No website linked on Google Maps. Who handles marketing or web stuff for {biz_name}?\n\n"
            f"Had a quick question about your listing."
        )
        scripts["reciprocity"] = (
            f"3-bullet inquiry page layout for {biz_name} since there's no website linked{loc_str}.\n\n"
            f"Worth dropping here, or keeping things referral-only?"
        )

    elif is_dead_domain:
        proof_asset = "quick screenshot"
        specific_flaw = "website link on Google profile isn't loading"

        # Slot 1
        scripts["naive_curiosity"] = (
            f"Site down on purpose?\n\n"
            f"Tried opening the Google Maps link for {biz_name} and it wouldn't load at all."
        )
        scripts["loss_aversion"] = (
            f"Broken website link on Google Maps. Tapped the site for {biz_name} and anyone tapping it hits an error and calls the next business.\n\n"
            f"Want a quick screenshot for whoever handles your site, or is the website on the backburner?"
        )

        # Slot 2
        scripts["accusation_audit"] = (
            f"Website link on Google Maps wouldn't load when checking {biz_name}. Probably sounds like another website pitch, so feel free to ignore this.\n\n"
            f"Took it down on purpose, or want a quick screenshot?"
        )
        scripts["no_oriented"] = (
            f"Google Maps site link wouldn't load when I tried opening {biz_name}.\n\n"
            f"Have you given up on keeping that site live, or is it still on your radar?"
        )

        # Slot 3
        if reviews_count >= 30:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but the website link for {biz_name} is dead on Google Maps. Anyone tapping it hits an error and calls the next business. Spotted this even though your reviews beat half the {city_or_local} listings next to you.\n\n"
                f"Took it down on purpose, or want a quick screenshot for whoever handles your site?"
            )
        elif reviews_count > 0:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but the website link for {biz_name} is dead on Google Maps. Anyone tapping it hits an error and calls the next business. Spotted this checking {biz_name}.\n\n"
                f"Took it down on purpose, or want a quick screenshot for whoever handles your site?"
            )
        else:
            scripts["local_contrast"] = (
                f"Dead website link on Google Maps compared to other {city_niche}. Anyone tapping {biz_name} hits an error and calls the next business.\n\n"
                f"Took it down on purpose, or want a quick screenshot for whoever handles your site?"
            )

        if reviews_count > 0:
            scripts["reputation_dissonance"] = (
                f"Website link isn't loading on Google, which doesn't match {rating_str}★ across {reviews_count} reviews for {biz_name}. Anyone tapping it hits an error and calls the next business.\n\n"
                f"Took it down on purpose, or mind if I send a quick screenshot?"
            )
        else:
            scripts["reputation_dissonance"] = (
                f"Website link isn't loading on Google Maps for {biz_name}{loc_str}. Anyone tapping it hits an error and calls the next business.\n\n"
                f"Took it down on purpose, or mind if I send a quick screenshot?"
            )

        # Slot 4
        scripts["gatekeeper"] = (
            f"Website link on Google Maps isn't loading. Who handles the site for {biz_name}?\n\n"
            f"Wanted to send them a quick screenshot so they can check it."
        )
        scripts["reciprocity"] = (
            f"Checked why the Google Maps site link for {biz_name} isn't loading.\n\n"
            f"Want me to send over what's failing so it's an easy fix, or did you take it down on purpose?"
        )

    elif chosen_flaw_key and variants:
        v_curiosity = variants["curiosity"]
        v_direct = variants["direct"]
        v_impact = variants["impact"]
        v_router = variants["router"]
        v_contrast = variants["contrast"]
        v_preview_hook = variants["preview_hook"]
        v_effect = variants["business_effect"]
        specific_flaw = f"{v_direct} on my phone" if chosen_flaw_key == "broken_mobile" else v_direct

        # Slot 1
        scripts["naive_curiosity"] = (
            f"Is it supposed to {v_curiosity}?\n\n"
            f"Pulled up {biz_name} on mobile while checking {city_niche} and wondered if an update broke it."
        )
        scripts["loss_aversion"] = (
            f"Leads are hitting {v_impact} when tapping {biz_name} on Google. {v_effect}\n\n"
            f"Want a {proof_asset} for whoever handles your site, or is the website on the backburner?"
        )

        # Slot 2
        scripts["accusation_audit"] = (
            f"{_cap(v_direct)} on mobile when checking {biz_name}. Probably sounds like another spam website message, so feel free to ignore this.\n\n"
            f"Already got someone fixing it, or cool if I send a {proof_asset}?"
        )
        scripts["no_oriented"] = (
            f"Spotted {v_impact} when pulling up {biz_name} on my phone.\n\n"
            f"Would you be against me sending a {proof_asset} so you can see what I'm seeing?"
        )

        # Slot 3
        if reviews_count >= 30:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but {v_contrast}. {v_effect} Spotted this on {biz_name}, even though your reviews beat half the {city_or_local} listings next to you.\n\n"
                f"Already got someone on that, or want a {proof_asset}?"
            )
        elif reviews_count > 0:
            scripts["local_contrast"] = (
                f"{rating_str}★ across {reviews_count} reviews, but {v_contrast}. {v_effect} Spotted this checking {biz_name}.\n\n"
                f"Already got someone on that, or want a {proof_asset}?"
            )
        else:
            scripts["local_contrast"] = (
                f"{v_preview_hook} when checking {biz_name}. {v_effect}\n\n"
                f"Already got someone on that, or want a {proof_asset}?"
            )

        if reviews_count > 0:
            scripts["reputation_dissonance"] = (
                f"{v_preview_hook}, which doesn't match {rating_str}★ across {reviews_count} Google reviews for {biz_name}. {v_effect}\n\n"
                f"Already got someone fixing that, or mind if I send a {proof_asset}?"
            )
        else:
            scripts["reputation_dissonance"] = (
                f"{v_preview_hook} when checking {biz_name}. {v_effect}\n\n"
                f"Already got someone on that, or mind if I send a {proof_asset}?"
            )

        # Slot 4
        scripts["gatekeeper"] = (
            f"{_cap(v_router)}. Who handles the website for {biz_name}?\n\n"
            f"Wanted to pass them a {proof_asset} so they can patch it."
        )
        scripts["reciprocity"] = (
            f"2-step fix for {v_impact}. Checked {biz_name} on mobile and jotted down what's causing it so you stop losing mobile visitors.\n\n"
            f"Want me to drop it here, or is the site on the backburner?"
        )

    else:
        # Healthy / 0 verified flaws / incomplete shell: NO fake fallbacks
        needs_manual_note = True
        specific_flaw = ""
        for k in [
            "naive_curiosity", "loss_aversion", "accusation_audit", "no_oriented",
            "local_contrast", "reputation_dissonance", "gatekeeper", "reciprocity"
        ]:
            scripts[k] = ""

    # Sanitize phone digits for WhatsApp
    digits = extract_phone_digits(lead)
    wa_base = f"https://wa.me/{digits}" if digits else None

    # Channel-aware smart recommendation
    has_dm_email = bool(lead.get("dm_email"))
    phone_val = lead.get("clean_phone") or lead.get("phone")

    # Treat phone as non-direct if line_type is Toll-Free or NANPA area code is Toll-Free
    is_toll_free = False
    line_type = (lead.get("line_type") or "").strip().lower()
    if line_type == "toll-free":
        is_toll_free = True
    elif phone_val:
        p_digits = re.sub(r"\D", "", str(phone_val))
        if len(p_digits) == 11 and p_digits.startswith("1"):
            p_area = p_digits[1:4]
        elif len(p_digits) == 10:
            p_area = p_digits[:3]
        else:
            p_area = ""
        if p_area in {"800", "833", "844", "855", "866", "877", "888"}:
            is_toll_free = True

    has_valid_phone = bool(phone_val and len(re.sub(r"\D", "", str(phone_val))) >= 10 and not is_toll_free)
    has_form_or_gen_only = (bool(lead.get("form_url")) or bool(lead.get("generic_email"))) and not has_dm_email and not has_valid_phone

    if has_form_or_gen_only:
        recommended_key = "gatekeeper"
        recommended_slot = "slot_4"
        rec_reason = "Form/Generic Email channel detected; routing inquiry directly to website administrator or owner."
    elif rating_val and rating_val >= 4.7 and reviews_count >= 30:
        recommended_key = "local_contrast"
        recommended_slot = "slot_3"
        rec_reason = f"High Social Proof ({rating_str}★ across {reviews_count} reviews) creates strong local contrast with technical flaw."
    elif is_dead_domain or is_no_website:
        recommended_key = "accusation_audit"
        recommended_slot = "slot_2"
        rec_reason = "Cold listing / missing website; accusation audit disarms spam defensiveness up front."
    else:
        recommended_key = "naive_curiosity"
        recommended_slot = "slot_1"
        rec_reason = "Natural curiosity angle creates open conversational curiosity without triggering defensiveness."

    approaches_meta = {
        "naive_curiosity": {"title": "Curiosity", "psychology": "Pattern Interrupt & Genuine Inquiry"},
        "loss_aversion": {"title": "Loss Aversion", "psychology": "Revenue Leak & Bounced Inquiries Risk"},
        "accusation_audit": {"title": "Accusation", "psychology": "Call Out the Elephant (Voss)"},
        "no_oriented": {"title": "No-Oriented", "psychology": "Autonomy & Protection (Voss)"},
        "local_contrast": {"title": "Local Contrast", "psychology": "Competitor Context in City"},
        "reputation_dissonance": {"title": "Reputation", "psychology": "5-Star Standard vs Mobile Lag"},
        "gatekeeper": {"title": "Gatekeeper", "psychology": "Neutral Referral & Route to Site Admin"},
        "reciprocity": {"title": "Reciprocity", "psychology": "Upfront Value & 2-Step Fix"}
    }

    approaches: Dict[str, Dict[str, Any]] = {}
    for k, text in scripts.items():
        w = len(text.split()) if text else 0
        wa_url = f"{wa_base}?text={urllib.parse.quote(text)}" if (wa_base and text) else None
        meta = approaches_meta.get(k, {"title": k.replace("_", " ").title(), "psychology": ""})
        approaches[k] = {
            "key": k,
            "title": meta["title"],
            "psychology": meta["psychology"],
            "text": text,
            "word_count": w,
            "wa_url": wa_url
        }

    slots = {
        "slot_1": {
            "title": "Curiosity",
            "primary": "naive_curiosity",
            "alt": "loss_aversion",
            "primary_label": "Curiosity",
            "alt_label": "Loss Aversion",
        },
        "slot_2": {
            "title": "Accusation",
            "primary": "accusation_audit",
            "alt": "no_oriented",
            "primary_label": "Accusation",
            "alt_label": "No-Oriented",
        },
        "slot_3": {
            "title": "Local Contrast",
            "primary": "local_contrast",
            "alt": "reputation_dissonance",
            "primary_label": "Local Contrast",
            "alt_label": "Reputation",
        },
        "slot_4": {
            "title": "Gatekeeper",
            "primary": "gatekeeper",
            "alt": "reciprocity",
            "primary_label": "Gatekeeper",
            "alt_label": "Reciprocity",
        }
    }

    primary_wa_url = approaches[recommended_key]["wa_url"] if (recommended_key in approaches) else None

    return {
        "recommended": recommended_key,
        "recommended_slot": recommended_slot,
        "recommended_reason": rec_reason,
        "specific_flaw": specific_flaw,
        "needs_manual_note": needs_manual_note,
        "digits": digits,
        "slots": slots,
        "approaches": approaches,
        "primary_wa_url": primary_wa_url
    }
