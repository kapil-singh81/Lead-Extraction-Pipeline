import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from typing import Dict, Any, List, Tuple, Optional
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright, Browser, BrowserContext

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import database, classifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def detect_cms(page_content: str) -> str:
    """Fingerprints CMS and page builders from HTML content."""
    content_lower = page_content.lower()
    if "elementor" in content_lower or "elementor-widget" in content_lower:
        return "WordPress (Elementor)"
    if "wp-content" in content_lower or "wp-includes" in content_lower:
        return "WordPress"
    if "static.wixstatic.com" in content_lower or "wix.com" in content_lower:
        return "Wix"
    if "static1.squarespace.com" in content_lower or "squarespace" in content_lower:
        return "Squarespace"
    if "cdn.shopify.com" in content_lower:
        return "Shopify"
    return "Custom / Other"


def decode_cloudflare_email(cf_hex: str) -> Optional[str]:
    """Decodes Cloudflare data-cfemail or /cdn-cgi/l/email-protection# hex string."""
    try:
        cf_hex = cf_hex.strip().split("#")[-1]
        if len(cf_hex) < 4:
            return None
        k = int(cf_hex[:2], 16)
        email = "".join(chr(int(cf_hex[i:i+2], 16) ^ k) for i in range(2, len(cf_hex), 2))
        return email if "@" in email else None
    except Exception:
        return None


def extract_emails(
    html_content: str,
    generic_prefixes: List[str],
    noise_tokens: Optional[List[str]] = None
) -> Tuple[List[str], List[str]]:
    """
    Extracts validated commercial emails from HTML text, mailto: links,
    and Cloudflare obfuscated elements.
    Filters out noise, Sentry telemetry UUIDs, and framework placeholders.

    Returns: (dm_or_direct_emails, generic_emails)
    """
    if noise_tokens is None:
        noise_tokens = [
            "sentry", "wixpress", "cloudflare", "google", "gstatic",
            "schema.org", "wordpress", "elementor", "example.com",
            "domain.com", "yourdomain.com", "email.com"
        ]
    invalid_suffixes = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js")

    candidates = set()

    # 1. Standard regex email extraction
    for m in re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,63}", html_content):
        candidates.add(m)

    # 2. mailto: links (stripping ?subject=... query parameters)
    for m in re.findall(r'href=["\']mailto:([^"\'\s>]+)', html_content, re.IGNORECASE):
        raw_email = m.split("?")[0].strip()
        unquoted = urllib.parse.unquote(raw_email).strip()
        if "@" in unquoted:
            candidates.add(unquoted)

    # 3. Cloudflare email protection decoding (data-cfemail and cdn-cgi)
    for cf_hex in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', html_content):
        decoded = decode_cloudflare_email(cf_hex)
        if decoded:
            candidates.add(decoded)

    for cf_path in re.findall(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)', html_content):
        decoded = decode_cloudflare_email(cf_path)
        if decoded:
            candidates.add(decoded)

    dm_emails = []
    generic_emails = []
    seen = set()

    for email in candidates:
        email_clean = email.lower().strip().rstrip(".,;:)")
        if not email_clean or email_clean in seen:
            continue
        if any(email_clean.endswith(ext) for ext in invalid_suffixes):
            continue

        parts = email_clean.split("@", 1)
        if len(parts) != 2:
            continue
        user_part, domain = parts

        # Reject hex telemetry hashes (e.g. Sentry error tracking IDs)
        if re.match(r"^[a-f0-9]{24,64}$", user_part):
            continue

        # Reject framework and analytics noise domains
        if any(token in domain for token in noise_tokens):
            continue

        # Reject version numbers inside domain
        if re.search(r"\d+\.\d+", domain):
            continue

        seen.add(email_clean)
        if any(email_clean.startswith(prefix) for prefix in generic_prefixes):
            generic_emails.append(email_clean)
        else:
            dm_emails.append(email_clean)

    return dm_emails, generic_emails


DEFAULT_SOCIAL_STOP_TOKENS = ["llc", "inc", "corp", "co", "ltd", "the", "and", "services", "group", "company"]


def extract_social_links(
    links: List[str],
    business_name: Optional[str] = None,
    clean_domain: Optional[str] = None,
    stop_tokens: Optional[List[str]] = None
) -> Dict[str, Optional[str]]:
    """
    Extracts clean, verified social profile URLs from anchor links or JSON-LD sameAs.
    Preserves Facebook profile.php?id=... query parameters.
    Filters out sharing widgets, intents, dialogs, and platform tracking URLs.
    Selects candidate with strongest token match to business_name/clean_domain when multiple exist.
    """
    candidates: Dict[str, List[str]] = {
        "facebook_url": [],
        "instagram_url": [],
        "linkedin_url": [],
        "twitter_url": [],
        "youtube_url": []
    }

    for raw_url in links:
        if not raw_url or not isinstance(raw_url, str):
            continue
        url_lower = raw_url.lower().strip()
        try:
            parsed = urllib.parse.urlparse(raw_url)
            netloc = parsed.netloc.lower().replace("www.", "")
        except Exception:
            continue

        # 1. Facebook
        if netloc in {"facebook.com", "fb.com", "fb.me"} or netloc.endswith((".facebook.com", ".fb.com")):
            if not any(x in url_lower for x in ["sharer", "share.php", "/dialog/", "plugins/like", "plugins/share", "/tr/", "/help/", "/policies"]):
                path = parsed.path.rstrip("/")
                if len(path.strip("/")) > 1 and not path.startswith("/share"):
                    # Preserve profile.php?id=...
                    if path.endswith("profile.php") and parsed.query:
                        clean = f"{parsed.scheme or 'https'}://{parsed.netloc}{path}?{parsed.query}"
                    else:
                        clean = f"{parsed.scheme or 'https'}://{parsed.netloc}{path}"
                    if clean not in candidates["facebook_url"]:
                        candidates["facebook_url"].append(clean)

        # 2. Instagram
        if netloc == "instagram.com" or netloc.endswith(".instagram.com"):
            if not any(x in url_lower for x in ["/p/", "/reel/", "/reels/", "/stories/", "/explore/", "/tv/", "/direct/", "/accounts/", "/legal"]):
                path = parsed.path.rstrip("/")
                parts = [p for p in path.split("/") if p]
                if parts and parts[0] not in {"about", "developer", "legal"}:
                    clean = f"{parsed.scheme or 'https'}://{parsed.netloc}/{parts[0]}"
                    if clean not in candidates["instagram_url"]:
                        candidates["instagram_url"].append(clean)

        # 3. LinkedIn (Company Page or Personal Profile)
        if netloc == "linkedin.com" or netloc.endswith(".linkedin.com"):
            if any(x in url_lower for x in ["/company/", "/in/"]):
                if not any(x in url_lower for x in ["sharearticle", "sharing"]):
                    path = parsed.path.rstrip("/")
                    clean = f"{parsed.scheme or 'https'}://{parsed.netloc}{path}"
                    if clean not in candidates["linkedin_url"]:
                        candidates["linkedin_url"].append(clean)

        # 4. Twitter / X
        if netloc in {"twitter.com", "x.com"} or netloc.endswith((".twitter.com", ".x.com")):
            if not any(x in url_lower for x in ["intent/tweet", "/share", "/search", "intent/", "/tos", "/privacy"]):
                path = parsed.path.rstrip("/")
                parts = [p for p in path.split("/") if p]
                if parts and parts[0] not in {"i", "intent", "search", "share"}:
                    clean = f"{parsed.scheme or 'https'}://{parsed.netloc}/{parts[0]}"
                    if clean not in candidates["twitter_url"]:
                        candidates["twitter_url"].append(clean)

        # 5. YouTube
        if netloc == "youtube.com" or netloc.endswith(".youtube.com"):
            path = parsed.path.rstrip("/")
            parts = [p for p in path.split("/") if p]
            if parts:
                non_channel = {
                    "watch", "embed", "playlist", "shorts", "results", "feed",
                    "gaming", "premium", "about", "ads", "creators", "t",
                    "howyoutubeworks", "redirect", "live", "clip", "hashtag",
                    "trends", "channel_switcher", "account", "logout", "signin"
                }
                if parts[0].lower() not in non_channel:
                    if any(k in path for k in ["/@", "/channel/", "/c/", "/user/"]) or len(parts) == 1:
                        clean = f"{parsed.scheme or 'https'}://{parsed.netloc}{path}"
                        if clean not in candidates["youtube_url"]:
                            candidates["youtube_url"].append(clean)

    # Score candidates against clean_domain and business_name tokens to pick contractor profile over third-parties
    target_tokens = set()
    if clean_domain:
        domain_base = clean_domain.split(".")[0].lower()
        target_tokens.add(domain_base)
        target_tokens.update(t for t in re.split(r"[-_.]", clean_domain.lower()) if len(t) >= 3)
    if business_name:
        active_stop_tokens = set(t.lower() for t in (stop_tokens if stop_tokens is not None else DEFAULT_SOCIAL_STOP_TOKENS))
        target_tokens.update(
            t for t in re.split(r"\W+", business_name.lower())
            if len(t) >= 3 and t not in active_stop_tokens
        )

    def score_candidate(cand_url: str) -> int:
        u_lower = cand_url.lower()
        return sum(1 for token in target_tokens if token in u_lower)

    socials: Dict[str, Optional[str]] = {}
    for platform, items in candidates.items():
        if not items:
            socials[platform] = None
        elif len(items) == 1 or not target_tokens:
            socials[platform] = items[0]
        else:
            sorted_items = sorted(items, key=lambda x: score_candidate(x), reverse=True)
            socials[platform] = sorted_items[0]

    return socials


def extract_whatsapp_info(
    links: List[str],
    html_content: str,
    clean_phone: Optional[str] = None,
    line_type: str = "Unknown"
) -> Tuple[Optional[str], bool, str]:
    """
    Detects on-site WhatsApp widgets, floating buttons, and direct wa.me/ links.
    If no widget/link is found on the website, synchronizes with NANPA mobile line-type registry.

    Returns:
        (whatsapp_url, is_active, source)
        source is one of: "website_link", "website_widget", "mobile_registry", "none"
        NOTE: is_active is True ONLY for verified on-site links/widgets (website_link, website_widget).
    """
    # 1. Direct anchor links inspection
    for raw_url in links:
        if not raw_url:
            continue
        url_lower = str(raw_url).lower().strip()
        if "wa.me/" in url_lower or "api.whatsapp.com/send" in url_lower or "web.whatsapp.com/send" in url_lower or "whatsapp://" in url_lower:
            clean = raw_url.strip()
            if not clean.startswith(("http://", "https://", "whatsapp://")):
                clean = f"https://{clean.lstrip('/')}"
            return clean, True, "website_link"

    # 2. Inspect HTML DOM for embedded WhatsApp chat widgets / buttons
    widget_patterns = [
        r'https?://(?:api\.)?whatsapp\.com/send\?[^"\'\s>]+',
        r'https?://wa\.me/[0-9+]+',
        r'whatsapp://send\?[^"\'\s>]+'
    ]
    for pattern in widget_patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            return match.group(0), True, "website_widget"

    # Check for presence of common WhatsApp widget signatures (Join.chat, WP WhatsApp, Elfsight, Chaty)
    widget_dom_signatures = [
        "joinchat", "wp-whatsapp", "elfsight-app-whatsapp", "chaty-widget", "whatsapp-widget",
        "class=\"whatsapp", "class='whatsapp", "id=\"whatsapp", "id='whatsapp"
    ]
    has_widget_signature = any(sig in html_content.lower() for sig in widget_dom_signatures)
    if has_widget_signature:
        if clean_phone:
            digits = re.sub(r"\D", "", clean_phone)
            if len(digits) == 10:
                digits = f"1{digits}"
            elif len(digits) == 11 and digits.startswith("1"):
                pass
            if len(digits) >= 10:
                return f"https://wa.me/{digits}", True, "website_widget"

    # 3. Validated Mobile Line Fallback (NANPA mobile registry)
    # Allows 1-click launch from cockpit, but is_active remains False (unverified on-site)
    if clean_phone and str(line_type).strip().lower() == "mobile":
        digits = re.sub(r"\D", "", clean_phone)
        if len(digits) == 10:
            digits = f"1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            pass
        if len(digits) >= 10:
            return f"https://wa.me/{digits}", False, "mobile_registry"

    return None, False, "none"


def extract_linkedin(html_content: str) -> Optional[str]:
    """Extracts direct personal LinkedIn profile URLs if present."""
    match = re.search(r"https?://(www\.)?linkedin\.com/(in|company)/[a-zA-Z0-9_-]+", html_content, re.IGNORECASE)
    return match.group(0) if match else None


def parse_json_ld(html_content: str) -> Dict[str, Any]:
    """
    Parses all Schema.org JSON-LD scripts on the page.
    Extracts sameAs URLs (official socials), emails, and decision maker info.
    """
    result: Dict[str, Any] = {
        "same_as": [],
        "emails": [],
        "dm_name": None,
        "dm_title": None
    }
    try:
        ld_scripts = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_content,
            re.DOTALL | re.IGNORECASE
        )
        for s in ld_scripts:
            try:
                data = json.loads(s.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    graph_items = item.get("@graph", []) if isinstance(item, dict) else []
                    all_candidates = [item] + (graph_items if isinstance(graph_items, list) else [])
                    for c in all_candidates:
                        if not isinstance(c, dict):
                            continue

                        # Extract sameAs URLs (official social profiles)
                        same_as = c.get("sameAs") or []
                        if isinstance(same_as, str):
                            same_as = [same_as]
                        if isinstance(same_as, list):
                            for sa in same_as:
                                if isinstance(sa, str) and sa.strip():
                                    result["same_as"].append(sa.strip())

                        def _add_clean_email(raw_val: Any):
                            if isinstance(raw_val, list):
                                for item in raw_val:
                                    _add_clean_email(item)
                                return
                            if not isinstance(raw_val, str):
                                return
                            val = raw_val.strip()
                            if not val:
                                return
                            val = re.sub(r"(?i)^mailto:", "", val).strip()
                            val = val.split("?")[0].strip()
                            try:
                                val = urllib.parse.unquote(val).strip()
                            except Exception:
                                pass
                            val = val.lower()
                            if "@" in val and val not in result["emails"]:
                                result["emails"].append(val)

                        # Extract direct or contactPoint emails
                        _add_clean_email(c.get("email"))

                        cp = c.get("contactPoint") or []
                        if isinstance(cp, dict):
                            cp = [cp]
                        if isinstance(cp, list):
                            for p in cp:
                                if isinstance(p, dict):
                                    _add_clean_email(p.get("email"))

                        # Decision Maker extraction
                        if not result["dm_name"]:
                            for role in ("founder", "employee", "member"):
                                person = c.get(role)
                                if isinstance(person, dict) and person.get("name"):
                                    title = person.get("jobTitle") or role.capitalize()
                                    result["dm_name"] = str(person["name"]).strip()
                                    result["dm_title"] = str(title).strip()
                                    break
                                elif isinstance(person, list):
                                    for p in person:
                                        if isinstance(p, dict) and p.get("name"):
                                            title = p.get("jobTitle") or role.capitalize()
                                            result["dm_name"] = str(p["name"]).strip()
                                            result["dm_title"] = str(title).strip()
                                            break
            except Exception:
                continue
    except Exception:
        pass

    return result


def extract_decision_maker(html_content: str) -> Tuple[Optional[str], Optional[str]]:
    """Extracts Decision Maker name and title from structured Schema.org JSON-LD."""
    data = parse_json_ld(html_content)
    return data.get("dm_name"), data.get("dm_title")


def classify_phone_line(clean_phone: Optional[str], db_path: Optional[str] = None) -> str:
    """Classifies phone numbers into Toll-Free, Mobile, Landline, or Local / Unclassified."""
    if not clean_phone:
        return "None"

    digits = re.sub(r"\D", "", clean_phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        area_code = digits[:3]
        nxx = digits[3:6]
        if area_code in {"800", "833", "844", "855", "866", "877", "888"}:
            return "Toll-Free"

        if db_path:
            conn = database.get_connection(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT line_type FROM phone_prefixes WHERE npa = ? AND nxx = ? LIMIT 1",
                    (area_code, nxx)
                )
                row = cursor.fetchone()
                if row and row["line_type"]:
                    return str(row["line_type"]).capitalize()
            except Exception:
                pass
            finally:
                conn.close()

        return "Local (US)"

    return "International / Other"


def discover_key_subpages(base_url: str, links: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Categorizes internal subpages from links discovered on the homepage.
    Filters to same-domain URLs only to find Contact, Quote, About, and Gallery pages.
    """
    parsed_base = urllib.parse.urlparse(base_url)
    base_domain = parsed_base.netloc.lower()
    if base_domain.startswith("www."):
        base_domain = base_domain[4:]

    subpages: Dict[str, str] = {}

    for item in links:
        raw_href = item.get("href") or ""
        text = (item.get("text") or "").lower()
        if not raw_href or not raw_href.startswith(("http://", "https://")):
            continue

        parsed = urllib.parse.urlparse(raw_href)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        if netloc != base_domain:
            continue

        path = parsed.path.lower().strip()
        if not path or path == "/" or path.endswith((".pdf", ".jpg", ".png", ".webp", ".zip")):
            continue

        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        # 1. Contact / Quote page
        if ("contact" not in subpages) and any(k in path or k in text for k in ["contact", "reach-us", "get-in-touch", "location", "quote", "estimate", "consultation"]):
            subpages["contact"] = clean_url

        # 2. Portfolio / Gallery / Projects page
        elif ("gallery" not in subpages) and any(k in path or k in text for k in ["gallery", "project", "portfolio", "our-work", "photo", "case-stud"]):
            subpages["gallery"] = clean_url

        # 3. About / Team page
        elif ("about" not in subpages) and any(k in path or k in text for k in ["about", "who-we-are", "our-company", "our-team", "story"]):
            subpages["about"] = clean_url

        # 4. Reviews / Testimonials page
        elif ("reviews" not in subpages) and any(k in path or k in text for k in ["review", "testimonial"]):
            subpages["reviews"] = clean_url

    return subpages


def _should_preserve_status(lead: Dict[str, Any]) -> bool:
    """
    Determines whether a lead's existing status and status_reason should be preserved
    during an audit / re-audit. Manual outreach, deferred, and manual triage decisions
    are preserved, while automated disqualifications (e.g. Healthy Website) can re-evaluate.
    """
    status = (lead.get("status") or "").strip().lower()
    if status in {"contacted", "replied", "won", "lost", "deferred"}:
        return True

    if status == "disqualified":
        manual_notes = (lead.get("manual_notes") or "").lower()
        status_reason = (lead.get("status_reason") or "").strip()
        reason_lower = status_reason.lower()

        if "[reviewed]" in manual_notes:
            return True
        if "manual" in reason_lower:
            return True

        automated_reasons = {
            "healthy website (no conversion opportunity)",
            "dead domain (no contact)",
            "missing business name"
        }
        if reason_lower not in automated_reasons:
            return True

    return False


async def audit_single_site(
    context: BrowserContext,
    lead: Dict[str, Any],
    config: Dict[str, Any],
    semaphore: asyncio.Semaphore
) -> None:
    """
    Audits speed, mobile responsiveness, bloat, conversion factors, and contacts for a single website.
    Features:
      1. Challenge / Bot security screen detection (Cloudflare, DDoS-Guard, SiteGround).
      2. Fast scroll down to trigger lazy loading of images, footer contact info, and social links.
      3. Deep subpage crawling (audits up to 2 key subpages: Contact/Quote and About/Gallery).
      4. Multi-channel social extraction (Facebook, Instagram, LinkedIn, Twitter/X, YouTube).
      5. Aggregated opportunity verification to eliminate false positive flaw flags.
    """
    auditor_cfg = config["auditor"]
    thresholds = auditor_cfg["thresholds"]
    generic_prefixes = auditor_cfg["generic_email_prefixes"]
    db_path = config["app"]["db_path"]

    form_embed_domains = auditor_cfg.get("form_embed_domains", [
        "typeform", "hubspot", "hsforms", "jotform", "cognitoforms", "gravityforms",
        "wpforms", "formstack", "formspree", "paperform", "tally.so", "fillout",
        "leadconnectorhq", "msgsndr", "calendly", "acuityscheduling", "roofr",
        "jobnimbus", "acculynx", "housecallpro", "podium", "wufoo", "docs.google.com/forms"
    ])
    form_keywords = auditor_cfg.get("form_keywords", [
        "contact", "quote", "estimate", "schedule", "book-now", "book-online",
        "book a", "booking", "appointment", "request", "get-started", "consultation",
        "reach-us", "get-in-touch"
    ])
    agency_credit_patterns = auditor_cfg.get("agency_credit_patterns", [
        "designed by", "powered by", "website by", "built by", "marketing by",
        "developed by", "seo by", "created by"
    ])
    email_noise_tokens = auditor_cfg.get("email_noise_tokens", None)
    trust_keywords = auditor_cfg.get("trust_keywords", [
        "insured", "licensed", "warranty", "guarantee", "certified", "certifications",
        "reviews", "bbb", "bonded", "accredited", "experience", "quality", "trusted",
        "ratings", "satisfaction", "family-owned", "locally-owned", "years"
    ])
    social_stop_tokens = auditor_cfg.get("social_stop_tokens", None)

    eval_cfg = {
        "embed_domains": form_embed_domains,
        "form_keywords": form_keywords,
        "credit_patterns": agency_credit_patterns,
        "trust_keywords": trust_keywords
    }

    lead_id = lead["id"]
    url = lead.get("website_url") or ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    total_bytes = 0
    capturing_homepage_bytes = True

    async with semaphore:
        logging.info(f"Auditing [{lead_id}]: {url}")
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        def on_response(response):
            nonlocal total_bytes
            if not capturing_homepage_bytes:
                return
            try:
                # Ignore HTTP 206 (streaming media range requests)
                if response.status == 206:
                    return

                # Ignore video/ and audio/ MIME types (e.g. background hero video streaming)
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type.startswith(("video/", "audio/")):
                    return

                cl = response.headers.get("content-length")
                if cl and cl.isdigit():
                    total_bytes += int(cl)
            except Exception:
                pass

        page.on("response", on_response)

        page_timeout_ms = auditor_cfg.get("page_timeout_ms", 25000)
        hydration_wait_ms = auditor_cfg.get("hydration_wait_ms", 4000)

        try:
            t_start = time.perf_counter()
            timed_out_load = False
            try:
                await page.goto(url, timeout=page_timeout_ms, wait_until="load")
            except Exception as nav_err:
                err_str = str(nav_err).lower()
                if "timeout" in err_str:
                    timed_out_load = True
                    logging.info(f"Page load timed out after {page_timeout_ms}ms for [{lead_id}]: {url}. Proceeding to evaluate rendered DOM...")
                else:
                    logging.info(f"Target [{lead_id}] unreachable / dead domain ({url}): {nav_err}")
                    phone_line_type = classify_phone_line(lead.get("clean_phone"), db_path=db_path)
                    lead_enriched = {**lead, "line_type": phone_line_type}
                    unreach_label = f"Dead Domain / Site Unreachable: {str(nav_err)[:50]}"
                    audit_payload = {
                        "audit_completed": True,
                        "has_website": True,
                        "is_unreachable": True,
                        "load_time_sec": -1.0,
                        "ttfb_ms": -1,
                        "page_weight_mb": 0.0,
                        "dom_nodes": 0,
                        "mobile_broken": 0,
                        "cms_detected": "Unknown",
                        "failed_major_test": 1,
                        "flaw_codes": ["dead_domain"],
                        "flaw_labels": [unreach_label],
                        "primary_flaw_label": unreach_label,
                        "line_type": phone_line_type
                    }
                    status, status_reason, priority_score = classifier.classify_lead(lead_enriched, audit_payload, config)
                    if _should_preserve_status(lead):
                        status = lead["status"]
                        status_reason = lead.get("status_reason") or status_reason
                    database.update_lead_audit(lead_id, audit_payload, status, status_reason, priority_score, db_path=db_path)
                    return

            wall_clock_elapsed = time.perf_counter() - t_start

            # Bounded hydration wait: wait for networkidle or poll until content stabilizes
            hydration_deadline = asyncio.get_event_loop().time() + (hydration_wait_ms / 1000.0)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(3000, hydration_wait_ms))
            except Exception:
                pass

            last_dom_count = -1
            last_text_len = -1
            while asyncio.get_event_loop().time() < hydration_deadline:
                try:
                    dom_stats = await page.evaluate("""() => {
                        const textLen = (document.body && document.body.innerText) ? document.body.innerText.trim().length : 0;
                        const nodeCount = document.querySelectorAll('*').length;
                        return { textLen, nodeCount };
                    }""")
                    curr_text_len = dom_stats.get("textLen", 0)
                    curr_node_count = dom_stats.get("nodeCount", 0)

                    if curr_text_len >= 200 and curr_node_count == last_dom_count and curr_text_len == last_text_len:
                        break

                    last_dom_count = curr_node_count
                    last_text_len = curr_text_len
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            # Freeze total_bytes after homepage load & hydration before scrolling or subpage visits
            capturing_homepage_bytes = False
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

            # Check if challenge / security interstitial screen
            challenge_indicators = [
                "robot challenge screen", "just a moment", "attention required",
                "security check", "verify you are human", "sgcaptcha",
                "one moment, please", "one moment", "ddos-guard",
                "checking your browser", "ray id:", "cloudflare"
            ]
            title = (await page.title()).lower()
            cur_url = page.url.lower()

            if any(ind in title for ind in challenge_indicators) or any(ind in cur_url for ind in [".well-known/sgcaptcha", ".well-known/captcha", "captcha.mysitearea", "/challenge-platform/"]):
                logging.info(f"Security challenge screen detected for [{lead_id}]: {url}. Waiting for resolution...")
                for _ in range(5):
                    await page.wait_for_timeout(1000)
                    title = (await page.title()).lower()
                    cur_url = page.url.lower()
                    if not any(ind in title for ind in challenge_indicators) and not any(ind in cur_url for ind in [".well-known/sgcaptcha", ".well-known/captcha", "captcha.mysitearea"]):
                        break

            # If still gated by host firewall or net error
            if any(ind in title for ind in challenge_indicators) or any(ind in cur_url for ind in [".well-known/sgcaptcha", ".well-known/captcha", "captcha.mysitearea"]) or "chrome-error:" in cur_url:
                logging.info(f"Target [{lead_id}] is gated by host security challenge / firewall ({page.url}).")
                phone_line_type = classify_phone_line(lead.get("clean_phone"), db_path=db_path)
                lead_enriched = {**lead, "line_type": phone_line_type}
                audit_payload = {
                    "audit_completed": True,
                    "has_website": True,
                    "is_unreachable": False,
                    "is_bot_gated": True,
                    "load_time_sec": 1.0,
                    "ttfb_ms": 200,
                    "page_weight_mb": 0.05,
                    "dom_nodes": 0,
                    "mobile_broken": 0,
                    "cms_detected": "Protected (Host Security / Cloudflare / SiteGround)",
                    "failed_major_test": 0,
                    "flaw_codes": ["bot_challenge_gated"],
                    "flaw_labels": ["Protected by Host Security / Bot Gate (Manual Verification Required)"],
                    "primary_flaw_label": "Protected by Host Security / Bot Gate (Manual Verification Required)",
                    "line_type": phone_line_type
                }
                status, status_reason, priority_score = classifier.classify_lead(lead_enriched, audit_payload, config)
                if _should_preserve_status(lead):
                    status = lead["status"]
                    status_reason = lead.get("status_reason") or status_reason
                database.update_lead_audit(lead_id, audit_payload, status, status_reason, priority_score, db_path=db_path)
                return

            # Scroll down to trigger lazy loading of images, footer content, social links, and forms
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await page.wait_for_timeout(300)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(400)
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            # Evaluate technical performance metrics on homepage
            metrics = await page.evaluate(r"""(cfg) => {
                const embedDomains = cfg.embed_domains || [];
                const formKeywords = cfg.form_keywords || [];
                const creditPatterns = cfg.credit_patterns || [];
                const trustKeywords = cfg.trust_keywords || [];

                const nav = performance.getEntriesByType('navigation')[0];
                let loadTimeSec = 0;
                let ttfbMs = 0;
                if (nav) {
                    if (nav.loadEventEnd > 0 && nav.startTime >= 0 && (nav.loadEventEnd - nav.startTime) > 0) {
                        loadTimeSec = (nav.loadEventEnd - nav.startTime) / 1000;
                    } else if (nav.duration > 0) {
                        loadTimeSec = nav.duration / 1000;
                    }
                    ttfbMs = Math.round(nav.responseStart - nav.requestStart);
                }
                const allNodes = document.querySelectorAll('*');
                const domNodes = allNodes.length;
                const mobileBroken = document.documentElement.scrollWidth > (window.innerWidth + 5);

                // Text content length measured from document.body
                let totalText = 0;
                if (document.body && document.body.innerText) {
                    totalText = document.body.innerText.trim().length;
                }

                // Meaningful project images (excluding 1x1 tracking pixels)
                const images = Array.from(document.querySelectorAll('img')).filter(img => {
                    const rect = img.getBoundingClientRect();
                    const isNotTiny = (img.naturalWidth > 50 || rect.width > 50 || img.width > 50) && (img.naturalHeight > 50 || rect.height > 50 || img.height > 50);
                    const src = (img.src || '').toLowerCase();
                    const isNotPixel = !src.includes('pixel') && !src.includes('analytics') && !src.includes('data:image/svg') && !src.includes('spacer');
                    return isNotTiny && isNotPixel;
                });

                // Background images on key container sections
                const bgElements = Array.from(document.querySelectorAll('header, section, div.hero, div.banner, div.slide, div.gallery')).filter(el => {
                    const bg = window.getComputedStyle(el).backgroundImage;
                    return bg && bg.startsWith('url(') && !bg.includes('data:image/svg');
                });
                const totalImageCount = images.length + bgElements.length;

                // Lead capture & tap-to-call checks
                const hasTelLink = document.querySelectorAll('a[href^="tel:"]').length > 0;

                // Detect embedded form iframe
                let formIframeSrc = null;
                const iframes = Array.from(document.querySelectorAll('iframe'));
                for (const ifr of iframes) {
                    const src = (ifr.src || '').toLowerCase();
                    if (embedDomains.some(d => src.includes(d))) {
                        formIframeSrc = ifr.src;
                        break;
                    }
                }

                // Detect real on-page form (excluding search-only forms)
                const forms = Array.from(document.querySelectorAll('form'));
                const hasRealForm = forms.some(f => {
                    const action = (f.getAttribute('action') || '').toLowerCase();
                    if (action.includes('search') || f.getAttribute('role') === 'search') return false;
                    const inputs = Array.from(f.querySelectorAll('input, textarea, select'));
                    const nonSearchInputs = inputs.filter(inp => {
                        const t = (inp.getAttribute('type') || 'text').toLowerCase();
                        const n = (inp.getAttribute('name') || '').toLowerCase();
                        return t !== 'search' && t !== 'hidden' && !n.includes('search') && n !== 's';
                    });
                    return nonSearchInputs.length > 0;
                });

                const hasDirectInputs = document.querySelectorAll('input[type="tel"], input[type="email"], textarea').length > 0;
                const hasOnPageFormOrEmbed = hasRealForm || hasDirectInputs || (formIframeSrc !== null);

                const interactive = Array.from(document.querySelectorAll('a, button, input[type="submit"]')).map(el => ((el.innerText || el.value || '')).toLowerCase());
                const hasCta = interactive.some(t => formKeywords.some(kw => t.includes(kw)));
                const hasLeadCapture = (hasOnPageFormOrEmbed || hasCta);

                // Sticky header screen takeover (ignore hidden elements, overlays, drawers, and modal backdrops)
                let maxStickyPct = 0;
                allNodes.forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'sticky') {
                        if (
                            style.display === 'none' ||
                            style.visibility === 'hidden' ||
                            parseFloat(style.opacity || '1') === 0 ||
                            style.pointerEvents === 'none'
                        ) {
                            return;
                        }
                        const rect = el.getBoundingClientRect();
                        if (rect.height <= 0) return;

                        if (
                            rect.top <= 10 &&
                            rect.width > (window.innerWidth * 0.8) &&
                            rect.height < (window.innerHeight * 0.60)
                        ) {
                            const pct = Math.round((rect.height / window.innerHeight) * 100);
                            if (pct > maxStickyPct) maxStickyPct = pct;
                        }
                    }
                });

                // Foundational trust anchors
                const bodyText = (document.body ? document.body.innerText || '' : '').toLowerCase();
                const trustSignals = trustKeywords.filter(kw => bodyText.includes(kw)).length;

                // Helper: detect if an anchor is inside an agency credit container
                const BLOCK_CONTAINER_TAGS = new Set(['BODY', 'MAIN', 'FOOTER', 'HEADER', 'NAV', 'SECTION', 'ASIDE']);
                function isAgencyCredit(a) {
                    let curr = a;
                    for (let i = 0; i < 3; i++) {
                        if (!curr) break;
                        if (i > 0 && BLOCK_CONTAINER_TAGS.has((curr.tagName || '').toUpperCase())) {
                            break;
                        }
                        const pText = (curr.innerText || curr.textContent || '').trim().toLowerCase();
                        const linkCount = curr.querySelectorAll ? curr.querySelectorAll('a[href]').length : 0;
                        if (i > 0 && (pText.length > 140 || linkCount > 3)) {
                            break;
                        }
                        if (creditPatterns.some(pat => pText.includes(pat))) {
                            return true;
                        }
                        curr = curr.parentElement;
                    }
                    return false;
                }

                // Extract all anchor links with text and credit flag
                const rawLinks = Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href,
                    text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                    is_credit: isAgencyCredit(a)
                }));

                // Form URL resolution
                let resolvedFormUrl = null;
                const sameDomainAnchors = [];
                const externalFormAnchors = [];
                const pageHost = window.location.hostname.replace(/^www\./, '').toLowerCase();

                for (const a of rawLinks) {
                    if (a.is_credit || !a.href || a.href.startsWith('tel:') || a.href.startsWith('mailto:') || a.href.startsWith('javascript:')) {
                        continue;
                    }
                    try {
                        const u = new URL(a.href, window.location.origin);
                        const aHost = u.hostname.replace(/^www\./, '').toLowerCase();
                        const fullStr = (u.pathname + u.search + ' ' + a.text).toLowerCase();
                        const isMatch = formKeywords.some(kw => fullStr.includes(kw));

                        if (isMatch) {
                            if (aHost === pageHost || aHost === '') {
                                sameDomainAnchors.push(a.href);
                            } else if (embedDomains.some(d => aHost.includes(d) || u.href.toLowerCase().includes(d))) {
                                externalFormAnchors.push(a.href);
                            }
                        }
                    } catch (e) {}
                }

                if (sameDomainAnchors.length > 0) {
                    resolvedFormUrl = sameDomainAnchors[0];
                } else if (formIframeSrc) {
                    resolvedFormUrl = formIframeSrc;
                } else if (hasRealForm || hasDirectInputs) {
                    resolvedFormUrl = window.location.href;
                } else if (externalFormAnchors.length > 0) {
                    resolvedFormUrl = externalFormAnchors[0];
                }

                return {
                    load_time_sec: Math.max(0, loadTimeSec),
                    ttfb_ms: Math.max(0, ttfbMs),
                    dom_nodes: domNodes,
                    mobile_broken: mobileBroken ? 1 : 0,
                    text_length: totalText,
                    image_count: totalImageCount,
                    has_lead_capture: hasLeadCapture ? 1 : 0,
                    has_tel_link: hasTelLink ? 1 : 0,
                    sticky_header_pct: maxStickyPct,
                    trust_signals: trustSignals,
                    form_url: resolvedFormUrl,
                    raw_links: rawLinks
                };
            }""", eval_cfg)

            try:
                html_content = await page.content()
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=3000)
                    html_content = await page.content()
                except Exception:
                    html_content = ""

            cms = detect_cms(html_content)

            # 1. Parse JSON-LD first for official socials, emails, and decision maker
            json_ld_data = parse_json_ld(html_content)
            dm_name = json_ld_data.get("dm_name")
            dm_title = json_ld_data.get("dm_title")

            # Extract emails with noise filtering
            dm_emails, generic_emails = extract_emails(html_content, generic_prefixes, noise_tokens=email_noise_tokens)

            # Validate and merge JSON-LD emails with highest priority
            raw_jsonld_emails = json_ld_data.get("emails", [])
            if raw_jsonld_emails:
                j_dm, j_gen = extract_emails(" ".join(raw_jsonld_emails), generic_prefixes, noise_tokens=email_noise_tokens)
                dm_emails = list(dict.fromkeys(j_dm + dm_emails))
                generic_emails = list(dict.fromkeys(j_gen + generic_emails))

            # Clean domain and business name for social token-overlap scoring
            business_name = lead.get("business_name") or ""
            clean_domain = lead.get("clean_domain") or ""

            # 2. Extract social links: JSON-LD sameAs first (highest priority), then non-credit page links
            same_as_links = json_ld_data.get("same_as", [])
            jsonld_socials = extract_social_links(same_as_links, business_name=business_name, clean_domain=clean_domain, stop_tokens=social_stop_tokens)

            non_credit_links = [
                l["href"] for l in metrics.get("raw_links", [])
                if l.get("href") and not l.get("is_credit")
            ]
            page_socials = extract_social_links(non_credit_links, business_name=business_name, clean_domain=clean_domain, stop_tokens=social_stop_tokens)

            socials = {}
            for platform in ["facebook_url", "instagram_url", "linkedin_url", "twitter_url", "youtube_url"]:
                socials[platform] = jsonld_socials.get(platform) or page_socials.get(platform)

            dm_linkedin = extract_linkedin(html_content)

            # Classify line type and detect WhatsApp availability
            phone_line_type = classify_phone_line(lead.get("clean_phone"), db_path=db_path)
            whatsapp_url, is_whatsapp_active, whatsapp_source = extract_whatsapp_info(
                non_credit_links, html_content, lead.get("clean_phone"), phone_line_type
            )

            # Discover key subpages for targeted multi-page auditing
            discovered_subpages = discover_key_subpages(url, metrics.get("raw_links", []))
            logging.info(f"[{lead_id}] Discovered subpages: {list(discovered_subpages.keys())}")

            # Pick up to 2 targeted subpages:
            # 1. Contact / Quote subpage (vital for forms, emails, and phone)
            # 2. Gallery / Portfolio or About subpage (vital for job photos and trust anchors)
            subpages_to_audit = []
            if "contact" in discovered_subpages:
                subpages_to_audit.append(("contact", discovered_subpages["contact"]))

            if "gallery" in discovered_subpages:
                subpages_to_audit.append(("gallery", discovered_subpages["gallery"]))
            elif "about" in discovered_subpages:
                subpages_to_audit.append(("about", discovered_subpages["about"]))
            elif "reviews" in discovered_subpages:
                subpages_to_audit.append(("reviews", discovered_subpages["reviews"]))

            # Execute targeted subpage audits within the same authenticated browser context
            for sub_category, sub_url in subpages_to_audit[:2]:
                try:
                    sub_page = await context.new_page()
                    try:
                        await sub_page.goto(sub_url, timeout=10000, wait_until="domcontentloaded")
                        await sub_page.wait_for_timeout(600)

                        sub_metrics = await sub_page.evaluate(r"""(cfg) => {
                            const embedDomains = cfg.embed_domains || [];
                            const creditPatterns = cfg.credit_patterns || [];

                            const BLOCK_CONTAINER_TAGS = new Set(['BODY', 'MAIN', 'FOOTER', 'HEADER', 'NAV', 'SECTION', 'ASIDE']);
                            function isAgencyCredit(a) {
                                let curr = a;
                                for (let i = 0; i < 3; i++) {
                                    if (!curr) break;
                                    if (i > 0 && BLOCK_CONTAINER_TAGS.has((curr.tagName || '').toUpperCase())) {
                                        break;
                                    }
                                    const pText = (curr.innerText || curr.textContent || '').trim().toLowerCase();
                                    const linkCount = curr.querySelectorAll ? curr.querySelectorAll('a[href]').length : 0;
                                    if (i > 0 && (pText.length > 140 || linkCount > 3)) {
                                        break;
                                    }
                                    if (creditPatterns.some(pat => pText.includes(pat))) {
                                        return true;
                                    }
                                    curr = curr.parentElement;
                                }
                                return false;
                            }

                            const links = Array.from(document.querySelectorAll('a[href]'))
                                .filter(a => !isAgencyCredit(a))
                                .map(a => a.href);

                            let formIframe = false;
                            for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
                                const src = (ifr.src || '').toLowerCase();
                                if (embedDomains.some(d => src.includes(d))) {
                                    formIframe = true;
                                    break;
                                }
                            }

                            // Detect real on-page form (excluding search-only forms)
                            const forms = Array.from(document.querySelectorAll('form'));
                            const hasRealForm = forms.some(f => {
                                const action = (f.getAttribute('action') || '').toLowerCase();
                                const role = (f.getAttribute('role') || '').toLowerCase();
                                if (role === 'search' || action.includes('search')) return false;
                                const inputs = Array.from(f.querySelectorAll('input, textarea, select'));
                                const nonSearchInputs = inputs.filter(inp => {
                                    const t = (inp.getAttribute('type') || 'text').toLowerCase();
                                    const n = (inp.getAttribute('name') || '').toLowerCase();
                                    return t !== 'search' && t !== 'hidden' && !n.includes('search') && n !== 's';
                                });
                                return nonSearchInputs.length > 0;
                            });

                            const hasDirectInputs = Array.from(document.querySelectorAll('input[type="tel"], input[type="email"], textarea')).some(el => {
                                const parentForm = el.closest('form');
                                if (parentForm) {
                                    const action = (parentForm.getAttribute('action') || '').toLowerCase();
                                    const role = (parentForm.getAttribute('role') || '').toLowerCase();
                                    if (role === 'search' || action.includes('search')) return false;
                                }
                                return true;
                            });

                            const hasForm = hasRealForm || hasDirectInputs || formIframe;

                            return {
                                links: links,
                                has_form: hasForm
                            };
                        }""", eval_cfg)

                        sub_links = sub_metrics.get("links", [])
                        sub_socials = extract_social_links(sub_links, business_name=business_name, clean_domain=clean_domain, stop_tokens=social_stop_tokens)
                        for k, v in sub_socials.items():
                            if not socials.get(k) and v:
                                socials[k] = v

                        sub_html = await sub_page.content()
                        s_dm_emails, s_gen_emails = extract_emails(sub_html, generic_prefixes, noise_tokens=email_noise_tokens)
                        dm_emails = list(dict.fromkeys(dm_emails + s_dm_emails))
                        generic_emails = list(dict.fromkeys(generic_emails + s_gen_emails))

                        # JSON-LD on subpage
                        sub_json_ld = parse_json_ld(sub_html)
                        for sa in sub_json_ld.get("same_as", []):
                            sa_socials = extract_social_links([sa], business_name=business_name, clean_domain=clean_domain, stop_tokens=social_stop_tokens)
                            for k, v in sa_socials.items():
                                if not socials.get(k) and v:
                                    socials[k] = v
                        if not dm_name and sub_json_ld.get("dm_name"):
                            dm_name = sub_json_ld["dm_name"]
                            dm_title = sub_json_ld.get("dm_title")

                        # Check for WhatsApp widgets/links on subpages
                        if not is_whatsapp_active:
                            sub_wa_url, sub_wa_active, sub_wa_source = extract_whatsapp_info(
                                sub_links, sub_html, lead.get("clean_phone"), phone_line_type
                            )
                            if sub_wa_active:
                                whatsapp_url, is_whatsapp_active, whatsapp_source = sub_wa_url, sub_wa_active, sub_wa_source

                        # Subpage form & contact checks
                        if sub_metrics.get("has_form"):
                            metrics["has_lead_capture"] = 1
                            if not metrics.get("form_url") or metrics.get("form_url") == url:
                                metrics["form_url"] = sub_url

                        sub_has_tel = await sub_page.evaluate("""() => document.querySelectorAll('a[href^="tel:"]').length > 0""")
                        if sub_has_tel:
                            metrics["has_tel_link"] = 1

                        # Subpage images & visual proof (excluding tracking pixels, analytics, and data URIs)
                        sub_images = await sub_page.evaluate("""() => {
                            const imgs = Array.from(document.querySelectorAll('img')).filter(img => {
                                const rect = img.getBoundingClientRect();
                                const isNotTiny = (img.naturalWidth > 50 || rect.width > 50 || img.width > 50) && (img.naturalHeight > 50 || rect.height > 50 || img.height > 50);
                                const src = (img.src || '').toLowerCase();
                                const isNotPixel = !src.includes('pixel') && !src.includes('analytics') && !src.includes('data:image/svg') && !src.includes('spacer');
                                return isNotTiny && isNotPixel;
                            });
                            return imgs.length;
                        }""")
                        metrics["image_count"] += sub_images

                        # Subpage trust signals (reusing trust_keywords from config)
                        sub_text = (await sub_page.evaluate("() => document.body ? document.body.innerText : ''")).lower()
                        sub_trust = sum(1 for kw in trust_keywords if kw in sub_text)
                        metrics["trust_signals"] += sub_trust

                    finally:
                        await sub_page.close()
                except Exception as sub_err:
                    logging.debug(f"Subpage audit skipped for {sub_url}: {sub_err}")

            page_weight_mb = round(total_bytes / (1024 * 1024), 2)
            if metrics.get("load_time_sec", 0) > 0:
                load_time_sec = round(metrics["load_time_sec"], 2)
            else:
                load_time_sec = round(wall_clock_elapsed, 2)

            if timed_out_load:
                load_time_sec = max(load_time_sec, round(wall_clock_elapsed, 2))

            ttfb_ms = metrics["ttfb_ms"]
            dom_nodes = metrics["dom_nodes"]
            mobile_broken = metrics["mobile_broken"]

            min_rendered_dom_nodes = thresholds.get("min_rendered_dom_nodes", 80)
            min_rendered_text_chars = thresholds.get("min_rendered_text_chars", 150)

            # Structured flaw detection mapping directly to config.json ranking weights
            flaw_codes: List[str] = []
            flaw_labels: List[str] = []

            # Check if page is an unrendered or bot-blocked shell
            if dom_nodes < min_rendered_dom_nodes or metrics["text_length"] < min_rendered_text_chars:
                page_title = await page.title()
                final_url = page.url
                logging.warning(f"Target [{lead_id}] render incomplete ({dom_nodes} DOM nodes, {metrics['text_length']} chars). Title: '{page_title}', URL: '{final_url}'")
                flaw_codes.append("render_incomplete")
                flaw_labels.append("Render Incomplete / Blocked Shell (Manual Verification Required)")
            else:
                if load_time_sec > thresholds["max_load_time_seconds"]:
                    flaw_codes.append("slow_load")
                    flaw_labels.append(f"Slow Load ({load_time_sec}s)")
                if ttfb_ms > thresholds["max_ttfb_ms"]:
                    flaw_codes.append("server_delay")
                    flaw_labels.append(f"Server Delay ({ttfb_ms}ms TTFB)")
                if page_weight_mb > thresholds["max_page_weight_mb"]:
                    flaw_codes.append("heavy_page")
                    flaw_labels.append(f"Heavy Page ({page_weight_mb}MB)")
                if dom_nodes > thresholds["max_dom_nodes"]:
                    flaw_codes.append("builder_bloat")
                    flaw_labels.append(f"Builder Bloat ({dom_nodes} DOM nodes)")
                if mobile_broken:
                    flaw_codes.append("broken_mobile")
                    flaw_labels.append("Broken Mobile Layout (Horizontal Overflow)")

                # Commercial conversion floors (aggregated across verified site pages)
                if dom_nodes < thresholds.get("min_dom_nodes", 250):
                    flaw_codes.append("thin_architecture")
                    flaw_labels.append(f"Thin Architecture ({dom_nodes} DOM nodes)")
                if metrics["text_length"] < thresholds.get("min_text_length", 600):
                    flaw_codes.append("content_starved")
                    flaw_labels.append("Content Starved (<600 chars)")
                if metrics["image_count"] < thresholds.get("min_images_count", 2):
                    flaw_codes.append("zero_visual_proof")
                    flaw_labels.append("Zero Visual Proof (No Job Photos)")
                if not metrics["has_lead_capture"]:
                    flaw_codes.append("missing_lead_capture")
                    flaw_labels.append("Missing Lead Capture (No Quote Form/CTA)")
                if not metrics["has_tel_link"]:
                    flaw_codes.append("unclickable_phone")
                    flaw_labels.append("Unclickable Phone (Missing tap-to-call link)")
                if metrics["sticky_header_pct"] > thresholds.get("max_sticky_header_pct", 25):
                    flaw_codes.append("obstructive_sticky_header")
                    flaw_labels.append(f"Obstructive Sticky Header ({metrics['sticky_header_pct']}% screen)")
                if metrics["trust_signals"] == 0:
                    flaw_codes.append("zero_trust_anchors")
                    flaw_labels.append("Zero Trust Anchors (No License/Insurance/Reviews)")

            failed_major_test = 1 if flaw_codes else 0
            primary_flaw_label = ", ".join(flaw_labels) if flaw_labels else "None"

            chosen_dm_email = dm_emails[0] if dm_emails else None
            chosen_generic_email = generic_emails[0] if generic_emails else None
            if not phone_line_type or phone_line_type == "Unknown":
                phone_line_type = classify_phone_line(lead.get("clean_phone"), db_path=db_path)

            existing_wa_src = (
                lead.get("whatsapp_source")
                or (lead.get("audit_json") if isinstance(lead.get("audit_json"), dict) else {}).get("whatsapp_source")
            )
            verified_wa_url = whatsapp_url if is_whatsapp_active else None
            if not verified_wa_url and lead.get("whatsapp_url"):
                if existing_wa_src != "mobile_registry" and whatsapp_source != "mobile_registry":
                    verified_wa_url = lead.get("whatsapp_url")

            audit_payload = {
                "audit_completed": True,
                "has_website": True,
                "is_unreachable": False,
                "load_time_sec": load_time_sec,
                "ttfb_ms": ttfb_ms,
                "page_weight_mb": page_weight_mb,
                "dom_nodes": dom_nodes,
                "text_length": metrics.get("text_length", 0),
                "mobile_broken": mobile_broken,
                "sticky_header_pct": metrics.get("sticky_header_pct", 0),
                "has_tel_link": metrics.get("has_tel_link", 0),
                "has_lead_capture": metrics.get("has_lead_capture", 0),
                "cms_detected": cms,
                "failed_major_test": failed_major_test,
                "flaw_codes": flaw_codes,
                "flaw_labels": flaw_labels,
                "primary_flaw_label": primary_flaw_label,
                "dm_name": dm_name or lead.get("dm_name"),
                "dm_title": dm_title or lead.get("dm_title"),
                "dm_email": chosen_dm_email or lead.get("dm_email"),
                "dm_linkedin": dm_linkedin or socials.get("linkedin_url") or lead.get("dm_linkedin"),
                "generic_email": chosen_generic_email or lead.get("generic_email"),
                "facebook_url": socials.get("facebook_url") or lead.get("facebook_url"),
                "instagram_url": socials.get("instagram_url") or lead.get("instagram_url"),
                "linkedin_url": socials.get("linkedin_url") or lead.get("linkedin_url"),
                "twitter_url": socials.get("twitter_url") or lead.get("twitter_url"),
                "youtube_url": socials.get("youtube_url") or lead.get("youtube_url"),
                "whatsapp_url": verified_wa_url,
                "is_whatsapp_active": bool(is_whatsapp_active),
                "whatsapp_source": whatsapp_source,
                "form_url": metrics.get("form_url") or lead.get("form_url"),
                "line_type": phone_line_type or lead.get("line_type")
            }

            lead_enriched = {
                **lead,
                "dm_name": dm_name or lead.get("dm_name"),
                "dm_title": dm_title or lead.get("dm_title"),
                "dm_email": chosen_dm_email or lead.get("dm_email"),
                "dm_linkedin": dm_linkedin or socials.get("linkedin_url") or lead.get("dm_linkedin"),
                "generic_email": chosen_generic_email or lead.get("generic_email"),
                "facebook_url": socials.get("facebook_url") or lead.get("facebook_url"),
                "instagram_url": socials.get("instagram_url") or lead.get("instagram_url"),
                "whatsapp_url": verified_wa_url,
                "form_url": metrics.get("form_url") or lead.get("form_url"),
                "line_type": phone_line_type or lead.get("line_type")
            }

            status, status_reason, priority_score = classifier.classify_lead(lead_enriched, audit_payload, config)
            if _should_preserve_status(lead):
                status = lead["status"]
                status_reason = lead.get("status_reason") or status_reason
            database.update_lead_audit(lead_id, audit_payload, status, status_reason, priority_score, db_path=db_path)
            logging.info(f"[Audit Completed] ID {lead_id} -> {status} ({status_reason}, Score: {priority_score}) | Flaws: {primary_flaw_label}")

        except Exception as e:
            logging.error(f"Unexpected audit error for ID {lead_id} ({url}): {e}", exc_info=True)
        finally:
            if not page.is_closed():
                await page.close()


def decode_bing_redirect(url: str) -> str:
    """
    Decodes Bing click-tracking redirects (e.g. /ck/a?...&u=a1<base64>...)
    back to the destination URL. Returns url unchanged if not a valid Bing redirect.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if (netloc == "bing.com" or netloc.endswith(".bing.com")) and parsed.path.startswith("/ck/a"):
            query_params = urllib.parse.parse_qs(parsed.query)
            u_vals = query_params.get("u")
            if u_vals and u_vals[0]:
                u_str = u_vals[0]
                if u_str.startswith("a1"):
                    raw_b64 = u_str[2:]
                    padding = (4 - len(raw_b64) % 4) % 4
                    if padding:
                        raw_b64 += "=" * padding
                    decoded = base64.urlsafe_b64decode(raw_b64.encode("ascii")).decode("utf-8", errors="ignore")
                    if decoded.startswith(("http://", "https://")):
                        return decoded
    except Exception:
        pass
    return url


def recover_missing_website(business_name: str, city: str, excluded_domains: Optional[List[str]] = None) -> Optional[str]:
    """
    Attempts to discover an unlisted official website for established businesses via Bing search.
    Filters out major directories and aggregators.
    """
    if not business_name:
        return None

    query = urllib.parse.quote_plus(f'"{business_name}" "{city}" official website')
    search_url = f"https://www.bing.com/search?q={query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    req = urllib.request.Request(search_url, headers=headers)

    directory_blacklist = {
        "yelp.com", "facebook.com", "yellowpages.com", "bbb.org",
        "angi.com", "thumbtack.com", "mapquest.com", "instagram.com",
        "linkedin.com", "twitter.com", "x.com", "wikipedia.org",
        "youtube.com", "bing.com", "microsoft.com", "yahoo.com"
    }
    if excluded_domains:
        directory_blacklist.update(d.lower().strip() for d in excluded_domains)

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        matches = re.findall(r'<li class="b_algo">.*?<h2><a href="([^"]+)"', html, re.DOTALL)
        for raw_link in matches:
            link = decode_bing_redirect(raw_link)
            netloc = urllib.parse.urlparse(link).netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if not any(d in netloc for d in directory_blacklist):
                return link
    except Exception:
        pass

    return None


def process_no_website_leads(config: Dict[str, Any]) -> int:
    """Classifies Path B leads (no website) directly without browser audit."""
    db_path = config["app"]["db_path"]
    excluded_domains = config.get("app", {}).get("excluded_domains", [])
    conn = database.get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, clean_phone, business_name, city, review_count, maps_url FROM leads
            WHERE (website_url IS NULL OR TRIM(website_url) = '' OR LOWER(TRIM(website_url)) IN ('none', 'n/a', 'null', 'undefined', 'unknown'))
              AND status = 'new'
        """)
        leads = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    count = 0
    for lead in leads:
        lead_id = lead["id"]
        business_name = lead.get("business_name") or ""
        city = lead.get("city") or ""
        review_count = lead.get("review_count") or 0
        phone = lead.get("clean_phone")

        # Attempt to recover missing website for established businesses
        if review_count >= 5:
            recovered_url = recover_missing_website(business_name, city, excluded_domains=excluded_domains)
            if recovered_url:
                netloc = urllib.parse.urlparse(recovered_url).netloc.lower()
                clean_domain = netloc[4:] if netloc.startswith("www.") else netloc
                rec_conn = database.get_connection(db_path)
                try:
                    with rec_conn:
                        rec_conn.cursor().execute(
                            "UPDATE leads SET website_url = ?, clean_domain = ? WHERE id = ?",
                            (recovered_url, clean_domain, lead_id)
                        )
                finally:
                    rec_conn.close()
                logging.info(f"Recovered unlisted website for '{business_name}': {recovered_url}")
                continue  # Skip marking as No Website; queued for normal audit

        phone_line_type = classify_phone_line(phone, db_path=db_path)
        lead_enriched = {**lead, "website_url": None, "line_type": phone_line_type}
        audit_payload = {
            "audit_completed": True,
            "has_website": False,
            "is_unreachable": False,
            "failed_major_test": 1,
            "flaw_codes": ["no_website"],
            "flaw_labels": ["No Website Present"],
            "primary_flaw_label": "No Website Present",
            "line_type": phone_line_type
        }
        status, status_reason, priority_score = classifier.classify_lead(lead_enriched, audit_payload, config)
        database.update_lead_audit(lead_id, audit_payload, status, status_reason, priority_score, db_path=db_path)
        count += 1

    if count > 0:
        logging.info(f"Classified {count} Path B leads (No Website).")
    return count


async def run_site_auditor(config, re_audit: bool = False, lead_id: Optional[int] = None) -> None:
    """Main executor to run parallel site audits on pending leads or re-audit existing leads."""
    if isinstance(config, str):
        with open(config, "r", encoding="utf-8") as f:
            config = json.load(f)

    db_path = config["app"]["db_path"]
    database.init_db(db_path)

    # 1. Process Path B leads (No Website) first if not auditing a specific lead and not re-auditing
    if not lead_id and not re_audit:
        await asyncio.to_thread(process_no_website_leads, config)

    # 2. Process Path A leads (Has Website)
    if lead_id:
        single_lead = database.get_lead_by_id(lead_id, db_path=db_path)
        leads_to_audit = [single_lead] if single_lead else []
    elif re_audit:
        leads_to_audit = database.get_leads_for_reaudit(db_path)
    else:
        leads_to_audit = database.get_unaudited_leads(db_path)

    leads_to_audit = [l for l in leads_to_audit if classifier.sanitize_url(l.get("website_url"))]
    if not leads_to_audit:
        logging.info("No websites pending audit.")
        return

    concurrency = config["auditor"].get("concurrency_limit", 5)
    logging.info(f"Found {len(leads_to_audit)} websites to audit (re_audit={re_audit}). Concurrency limit: {concurrency}")
    semaphore = asyncio.Semaphore(concurrency)

    auditor_cfg = config.get("auditor", {})
    launch_args = auditor_cfg.get(
        "headless_args",
        [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu"
        ]
    )
    mobile_user_agent = auditor_cfg.get(
        "mobile_user_agent",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
    )
    mobile_viewport = auditor_cfg.get(
        "mobile_viewport",
        {"width": 390, "height": 844}
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=launch_args
        )
        context = await browser.new_context(
            user_agent=mobile_user_agent,
            viewport=mobile_viewport,
            is_mobile=True,
            has_touch=True,
            locale="en-US"
        )
        tasks = [audit_single_site(context, lead, config, semaphore) for lead in leads_to_audit]
        await asyncio.gather(*tasks)
        await context.close()
        await browser.close()

    logging.info("--- Site Audits & Routing Pass Complete ---")


# Alias for pipeline executor
run_audit_pipeline = run_site_auditor


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Site Auditor")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--re-audit", action="store_true", help="Re-audit all existing leads in the database that have websites")
    parser.add_argument("--lead-id", type=int, default=None, help="Re-audit a specific lead ID")
    args = parser.parse_args()

    async def _standalone_run():
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        await run_site_auditor(cfg, re_audit=args.re_audit, lead_id=args.lead_id)

    asyncio.run(_standalone_run())
