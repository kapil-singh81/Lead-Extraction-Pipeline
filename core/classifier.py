import re
from typing import Dict, Any, Tuple, Optional, List

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,63}$")


def sanitize_text(val: Any) -> Optional[str]:
    """Sanitizes text, stripping whitespace and rejecting null-like tokens."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in {"none", "n/a", "null", "undefined", "unknown", "nan", "[]", "{}", "nil"}:
        return None
    return s


def sanitize_email(val: Any) -> Optional[str]:
    """Validates and normalizes email string. Rejects invalid formats and template placeholders."""
    s = sanitize_text(val)
    if not s:
        return None
    clean = s.lower()
    if EMAIL_REGEX.match(clean) and not any(clean.endswith(ext) for ext in (".png", ".jpg", ".webp", ".js", ".css")):
        return clean
    return None


def sanitize_url(val: Any) -> Optional[str]:
    """Validates URL structure with http/https scheme and domain dot."""
    s = sanitize_text(val)
    if not s:
        return None
    clean = s.lower()
    if clean.startswith(("http://", "https://")) and "." in clean.split("://", 1)[-1]:
        return s
    return None


def sanitize_phone(val: Any, min_digits: int = 10) -> Optional[str]:
    """Extracts numeric digits and enforces minimum digit threshold."""
    s = sanitize_text(val)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= min_digits:
        return s
    return None


def calculate_priority_score(lead: Dict[str, Any], audit_data: Dict[str, Any], config: Dict[str, Any]) -> float:
    """
    Computes priority score based on structured flaw_codes and verified outreach assets.
    Reads all weights and base bonus/penalty scores dynamically from config.json.
    """
    queue_cfg = config.get("queue", {})
    weights = queue_cfg.get("ranking_weights", {})
    base_scores = queue_cfg.get("base_scores", {})

    score = 0.0

    # 1. Structured flaw code scoring (pure key lookups, zero string parsing)
    flaw_codes: List[str] = audit_data.get("flaw_codes", [])
    for code in flaw_codes:
        score += weights.get(code, 0.0)

    # 2. Verified contact bonuses
    dm_email = sanitize_email(lead.get("dm_email"))
    generic_email = sanitize_email(lead.get("generic_email"))
    if dm_email or generic_email:
        score += weights.get("has_email_bonus", 2.0)

    line_type = sanitize_text(lead.get("line_type")) or "Unknown"
    if line_type == "Mobile":
        score += weights.get("has_mobile_phone_bonus", 1.5)
    elif line_type == "Toll-Free":
        score += base_scores.get("toll_free_penalty", -3.0)

    # WhatsApp availability bonus
    has_wa = bool(audit_data.get("is_whatsapp_active") or lead.get("whatsapp_url") or audit_data.get("whatsapp_url"))
    if has_wa:
        score += weights.get("has_whatsapp_bonus", 1.5)

    # 3. Established review authority bonus
    rev_count = lead.get("review_count") or 0
    max_bonus = base_scores.get("max_review_bonus", 5.0)
    score += min(max_bonus, rev_count * weights.get("review_count_factor", 0.02))

    return round(max(0.0, score), 2)


def classify_lead(lead: Dict[str, Any], audit_data: Dict[str, Any], config: Dict[str, Any]) -> Tuple[str, str, float]:
    """
    Single deterministic classification gate.
    Evaluates sanitized lead attributes and structured audit metrics against declarative config rules.

    Returns:
        (status, status_reason, priority_score)
    """
    queue_cfg = config.get("queue", {})
    base_scores = queue_cfg.get("base_scores", {})
    req_fields = queue_cfg.get("required_outreach_fields", {})
    min_phone_digits = base_scores.get("min_phone_digits", 10)

    business_name = sanitize_text(lead.get("business_name"))
    website_url = sanitize_url(lead.get("website_url"))

    # Gate 1: Business Name Presence
    if req_fields.get("require_business_name", True) and not business_name:
        return "disqualified", "Missing Business Name", 0.0

    # Gate 2: Audit Completion Verification (Defect 1 Fix)
    audit_completed = bool(audit_data.get("audit_completed", False))
    if req_fields.get("require_completed_audit", True) and not audit_completed:
        # Never evaluate or disqualify an un-audited lead
        return "new", "Pending Audit Completion", 0.0

    # Extract validated contacts
    dm_email = sanitize_email(lead.get("dm_email"))
    generic_email = sanitize_email(lead.get("generic_email"))
    clean_phone = sanitize_phone(lead.get("clean_phone"), min_phone_digits)
    form_url = sanitize_url(lead.get("form_url"))
    fb_url = sanitize_url(lead.get("facebook_url"))
    ig_url = sanitize_url(lead.get("instagram_url"))
    wa_url = sanitize_url(lead.get("whatsapp_url")) or sanitize_url(audit_data.get("whatsapp_url"))
    line_type = sanitize_text(lead.get("line_type")) or "Unknown"

    # Toll-free qualification rule
    is_toll_free = (line_type == "Toll-Free")
    exclude_toll_free = base_scores.get("exclude_toll_free_from_outreach", True)
    phone_actionable = bool(clean_phone and not (is_toll_free and exclude_toll_free))

    validated_contacts = {
        "dm_email": dm_email,
        "generic_email": generic_email,
        "clean_phone": clean_phone if phone_actionable else None,
        "form_url": form_url,
        "facebook_url": fb_url,
        "instagram_url": ig_url,
        "whatsapp_url": wa_url
    }

    # Verify at least one contact channel from config
    required_channels = req_fields.get("at_least_one_contact", ["clean_phone", "dm_email", "generic_email", "form_url"])
    has_actionable_contact = any(bool(validated_contacts.get(ch)) for ch in required_channels)

    is_unreachable = bool(audit_data.get("is_unreachable", False))
    flaw_codes = audit_data.get("flaw_codes", [])

    # Gate 3: Site Unreachable / Dead Domain
    if is_unreachable:
        if phone_actionable:
            score = base_scores.get("dead_domain_score", 5.0)
            return "outreach_ready", "Dead Domain (Active Phone)", score
        return "disqualified", "Dead Domain (No Contact)", 0.0

    # Gate 4: No Website (Path B)
    if not website_url:
        if phone_actionable:
            score = base_scores.get("no_website_score", 4.0)
            return "outreach_ready", "No Website (Active Phone)", score
        return "missing_info", "No Website & No Actionable Phone", 0.0

    # Gate 5: Healthy Website (Defect 2 Fix - Completed audit with 0 opportunity flaws)
    if not flaw_codes and not audit_data.get("failed_major_test", 0):
        return "disqualified", "Healthy Website (No Conversion Opportunity)", 0.0

    # Gate 6: Flawed Website / Host Challenge + Actionable Contact -> OUTREACH READY
    priority_score = calculate_priority_score(lead, audit_data, config)
    if has_actionable_contact:
        primary_ch = "Email" if (dm_email or generic_email) else ("WhatsApp" if wa_url else ("Phone" if phone_actionable else ("Social" if (fb_url or ig_url) else "Web Form")))
        if "bot_challenge_gated" in flaw_codes:
            return "outreach_ready", f"Host Security Gated ({primary_ch} Available)", priority_score
        return "outreach_ready", f"Flawed Site ({primary_ch} Available)", priority_score

    # Gate 7: Flawed Website with Missing Contact Info
    return "missing_info", "Opportunity Detected (Missing Actionable Contact)", 0.0
