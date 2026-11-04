import asyncio
import json
import logging
import re
from typing import Dict, Any, List, Tuple, Optional
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright, Browser
import database

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


def extract_emails(html_content: str, generic_prefixes: List[str]) -> Tuple[List[str], List[str]]:
    """
    Extracts emails from text, filtering noise and punctuation.

    Filters applied (in order):
      1. Regex enforces a purely alphabetical TLD of 2–63 characters.
      2. Rejects matches whose domain contains a version-number pattern (e.g. "1.2").
      3. Blacklists known dummy / template domains.
      4. Strips trailing punctuation and deduplicates.

    Returns: (dm_or_direct_emails, generic_emails)
    """
    raw_matches = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,63}", html_content)

    DOMAIN_BLACKLIST = {
        "example.com",
        "yourdomain.com",
        "domain.com",
        "email.com",
        "sentry.io",
        "wixpress.com",
    }

    dm_emails = []
    generic_emails = []
    seen = set()
    invalid_suffixes = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js")

    for email in raw_matches:
        email_clean = email.lower().strip().rstrip(".,;:)")
        if not email_clean or email_clean in seen:
            continue
        if any(email_clean.endswith(ext) for ext in invalid_suffixes):
            continue

        # Reject if the domain portion contains a version-number pattern (e.g. "2.0", "1.14")
        domain = email_clean.split("@", 1)[1]
        if re.search(r"\d+\.\d+", domain):
            continue

        # Reject blacklisted dummy / template domains
        if domain in DOMAIN_BLACKLIST:
            continue

        seen.add(email_clean)
        if any(email_clean.startswith(prefix) for prefix in generic_prefixes):
            generic_emails.append(email_clean)
        else:
            dm_emails.append(email_clean)

    return dm_emails, generic_emails


def extract_linkedin(html_content: str) -> Optional[str]:
    """Extracts direct personal LinkedIn profile URLs if present."""
    match = re.search(r"https?://(www\.)?linkedin\.com/in/[a-zA-Z0-9_-]+", html_content, re.IGNORECASE)
    return match.group(0) if match else None


def extract_decision_maker(html_content: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts Decision Maker name and title exclusively from structured Schema.org JSON-LD metadata.
    Leaves fields null if not present in structured metadata (anti-scope creep).
    Returns: (dm_name, dm_title)
    """
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
                        for role in ("founder", "employee", "member"):
                            person = c.get(role)
                            if isinstance(person, dict) and person.get("name"):
                                title = person.get("jobTitle") or role.capitalize()
                                return str(person["name"]).strip(), str(title).strip()
                            elif isinstance(person, list):
                                for p in person:
                                    if isinstance(p, dict) and p.get("name"):
                                        title = p.get("jobTitle") or role.capitalize()
                                        return str(p["name"]).strip(), str(title).strip()
            except Exception:
                continue
    except Exception:
        pass

    return None, None


def determine_lead_status(
    has_major_flaw: bool,
    is_unreachable: bool,
    clean_phone: Optional[str],
    dm_email: Optional[str],
    dm_linkedin: Optional[str],
    generic_email: Optional[str]
) -> Tuple[str, str]:
    """Determines lead qualification bucket based on flaws and available contact channels."""
    if not has_major_flaw and not is_unreachable:
        return "Disqualified", "Healthy Site"

    if is_unreachable:
        if clean_phone and len(clean_phone) >= 7:
            return "Outreach Ready", "Dead Domain (Phone Active)"
        return "Disqualified", "Dead Domain (No Contact)"

    has_phone = bool(clean_phone and len(clean_phone) >= 7)
    has_direct_digital = bool(dm_email or dm_linkedin)

    if has_direct_digital:
        return "Outreach Ready", "Email / LinkedIn Available"
    if has_phone:
        return "Outreach Ready", "Direct Phone Available"
    if generic_email:
        return "Missing Info", "Needs DM Contact (Generic Email Only)"
    return "Missing Info", "Needs Contact Info"


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

        # Safe query if an offline prefix table exists in SQLite
        if db_path:
            try:
                with database.get_connection(db_path) as conn:
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

        return "Local (US)"

    return "International / Other"


async def audit_single_site(
    browser: Browser,
    lead: Dict[str, Any],
    config: Dict[str, Any],
    semaphore: asyncio.Semaphore
) -> None:
    """
    Audits speed, mobile responsiveness, bloat, and contacts for a single website.
    Strictly evaluates single landing URL without secondary crawl overhead.
    """
    auditor_cfg = config["auditor"]
    thresholds = auditor_cfg["thresholds"]
    generic_prefixes = auditor_cfg["generic_email_prefixes"]
    db_path = config["app"]["db_path"]

    lead_id = lead["id"]
    url = lead["website_url"]
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    total_bytes = 0

    async with semaphore:
        logging.info(f"Auditing [{lead_id}]: {url}")
        context = await browser.new_context(
            viewport=auditor_cfg.get("mobile_viewport", {"width": 390, "height": 844}),
            user_agent=auditor_cfg.get("mobile_user_agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"),
            ignore_https_errors=True
        )
        page = await context.new_page()

        def on_response(response):
            nonlocal total_bytes
            try:
                cl = response.headers.get("content-length")
                if cl and cl.isdigit():
                    total_bytes += int(cl)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, timeout=auditor_cfg.get("page_timeout_ms", 15000), wait_until="domcontentloaded")
            await page.wait_for_timeout(800)

            metrics = await page.evaluate("""() => {
                const nav = performance.getEntriesByType('navigation')[0];
                let loadTimeSec = 0;
                let ttfbMs = 0;
                if (nav) {
                    loadTimeSec = (nav.loadEventEnd > 0 ? (nav.loadEventEnd - nav.startTime) : nav.duration) / 1000;
                    ttfbMs = Math.round(nav.responseStart - nav.requestStart);
                }
                const allNodes = document.querySelectorAll('*');
                const domNodes = allNodes.length;
                const mobileBroken = document.documentElement.scrollWidth > (window.innerWidth + 5);

                // Text content length
                const textEls = document.querySelectorAll('p, li, h1, h2, h3, h4, span');
                let totalText = 0;
                textEls.forEach(el => totalText += (el.innerText ? el.innerText.trim().length : 0));

                // Meaningful project images (excluding 1x1 tracking pixels and tiny icons)
                const images = Array.from(document.querySelectorAll('img')).filter(img => {
                    const rect = img.getBoundingClientRect();
                    return rect.width > 150 && rect.height > 150;
                });

                // Lead capture & tap-to-call checks
                const hasTelLink = document.querySelectorAll('a[href^="tel:"]').length > 0;
                const hasForm = document.querySelectorAll('form, input[type="tel"], input[type="email"]').length > 0;
                const ctaKeywords = ['quote', 'estimate', 'schedule', 'book', 'contact'];
                const interactive = Array.from(document.querySelectorAll('a, button')).map(el => (el.innerText || '').toLowerCase());
                const hasCta = interactive.some(t => ctaKeywords.some(kw => t.includes(kw)));
                const hasLeadCapture = (hasTelLink || hasForm || hasCta);

                // Sticky header screen takeover
                let maxStickyPct = 0;
                allNodes.forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'sticky') {
                        const rect = el.getBoundingClientRect();
                        if (rect.top <= 10 && rect.width > (window.innerWidth * 0.8)) {
                            const pct = Math.round((rect.height / window.innerHeight) * 100);
                            if (pct > maxStickyPct) maxStickyPct = pct;
                        }
                    }
                });

                // Foundational trust anchors
                const bodyText = (document.body.innerText || '').toLowerCase();
                const trustKeywords = ['insured', 'licensed', 'warranty', 'guarantee', 'certified', 'reviews', 'bbb'];
                const trustSignals = trustKeywords.filter(kw => bodyText.includes(kw)).length;

                // Extract direct Facebook business link (excluding share dialogs)
                const fbAnchor = Array.from(document.querySelectorAll('a[href*="facebook.com"]'))
                    .find(a => {
                        const h = a.href.toLowerCase();
                        return !h.includes('sharer') && !h.includes('share.php') && !h.includes('/dialog/');
                    });
                const facebookUrl = fbAnchor ? fbAnchor.href : null;

                // Extract direct Contact/Quote/Estimate page link
                const formKeywords = ['contact', 'quote', 'estimate', 'schedule', 'get-started'];
                const formAnchor = Array.from(document.querySelectorAll('a')).find(a => {
                    const h = a.href.toLowerCase();
                    return formKeywords.some(kw => h.includes(kw));
                });
                const formUrl = formAnchor ? formAnchor.href : null;

                return {
                    load_time_sec: Math.max(0, loadTimeSec),
                    ttfb_ms: Math.max(0, ttfbMs),
                    dom_nodes: domNodes,
                    mobile_broken: mobileBroken ? 1 : 0,
                    text_length: totalText,
                    image_count: images.length,
                    has_lead_capture: hasLeadCapture ? 1 : 0,
                    has_tel_link: hasTelLink ? 1 : 0,
                    sticky_header_pct: maxStickyPct,
                    trust_signals: trustSignals,
                    facebook_url: facebookUrl,
                    form_url: formUrl
                };
            }""")

            html_content = await page.content()
            cms = detect_cms(html_content)
            dm_emails, generic_emails = extract_emails(html_content, generic_prefixes)
            dm_linkedin = extract_linkedin(html_content)
            dm_name, dm_title = extract_decision_maker(html_content)

            page_weight_mb = round(total_bytes / (1024 * 1024), 2)
            load_time_sec = round(metrics["load_time_sec"], 2)
            ttfb_ms = metrics["ttfb_ms"]
            dom_nodes = metrics["dom_nodes"]
            mobile_broken = metrics["mobile_broken"]

            failures = []
            if load_time_sec > thresholds["max_load_time_seconds"]:
                failures.append(f"Slow Load ({load_time_sec}s)")
            if ttfb_ms > thresholds["max_ttfb_ms"]:
                failures.append(f"Server Delay ({ttfb_ms}ms TTFB)")
            if page_weight_mb > thresholds["max_page_weight_mb"]:
                failures.append(f"Heavy Page ({page_weight_mb}MB)")
            if dom_nodes > thresholds["max_dom_nodes"]:
                failures.append(f"Builder Bloat ({dom_nodes} DOM nodes)")
            if mobile_broken:
                failures.append("Broken Mobile Layout (Horizontal Overflow)")

            # Commercial Conversion Floors (Safe default fallbacks)
            if dom_nodes < thresholds.get("min_dom_nodes", 250):
                failures.append(f"Thin Architecture ({dom_nodes} DOM nodes)")
            if metrics["text_length"] < thresholds.get("min_text_length", 600):
                failures.append("Content Starved (<600 chars)")
            if metrics["image_count"] < thresholds.get("min_images_count", 2):
                failures.append("Zero Visual Proof (No Job Photos)")
            if not metrics["has_lead_capture"]:
                failures.append("Missing Lead Capture (No Quote Form/CTA)")
            if not metrics["has_tel_link"]:
                failures.append("Unclickable Phone (Missing tap-to-call link)")
            if metrics["sticky_header_pct"] > thresholds.get("max_sticky_header_pct", 25):
                failures.append(f"Obstructive Sticky Header ({metrics['sticky_header_pct']}% screen)")
            if metrics["trust_signals"] == 0:
                failures.append("Zero Trust Anchors (No License/Insurance/Reviews)")

            failed_major_test = 1 if failures else 0
            primary_flaw_label = ", ".join(failures) if failures else "None"

            chosen_dm_email = dm_emails[0] if dm_emails else None
            chosen_generic_email = generic_emails[0] if generic_emails else None

            primary_status, sub_status = determine_lead_status(
                has_major_flaw=bool(failed_major_test),
                is_unreachable=False,
                clean_phone=lead.get("clean_phone"),
                dm_email=chosen_dm_email,
                dm_linkedin=dm_linkedin,
                generic_email=chosen_generic_email
            )

            audit_payload = {
                "load_time_sec": load_time_sec,
                "ttfb_ms": ttfb_ms,
                "page_weight_mb": page_weight_mb,
                "dom_nodes": dom_nodes,
                "mobile_broken": mobile_broken,
                "cms_detected": cms,
                "failed_major_test": failed_major_test,
                "primary_flaw_label": primary_flaw_label,
                "dm_name": dm_name,
                "dm_title": dm_title,
                "dm_email": chosen_dm_email,
                "dm_linkedin": dm_linkedin,
                "generic_email": chosen_generic_email,
                "facebook_url": metrics.get("facebook_url"),
                "form_url": metrics.get("form_url")
            }

            database.update_lead_audit(lead_id, audit_payload, primary_status, sub_status, db_path=db_path)
            logging.info(f"[Audit Completed] ID {lead_id} -> {primary_status} ({sub_status}) | Flaws: {primary_flaw_label}")

        except Exception as e:
            logging.warning(f"Audit failed for ID {lead_id} ({url}): {e}")
            payload = {
                "load_time_sec": -1.0,
                "ttfb_ms": -1,
                "page_weight_mb": 0.0,
                "dom_nodes": 0,
                "mobile_broken": 0,
                "failed_major_test": 1,
                "primary_flaw_label": f"Site Unreachable: {str(e)[:50]}"
            }
            primary_status, sub_status = determine_lead_status(
                has_major_flaw=True,
                is_unreachable=True,
                clean_phone=lead.get("clean_phone"),
                dm_email=None,
                dm_linkedin=None,
                generic_email=None
            )
            database.update_lead_audit(lead_id, payload, primary_status, sub_status, db_path=db_path)
        finally:
            await context.close()


def recover_missing_website(business_name: str, city: str) -> Optional[str]:
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

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract organic search result links from Bing HTML
        matches = re.findall(r'<li class="b_algo">.*?<h2><a href="([^"]+)"', html, re.DOTALL)
        for link in matches:
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
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, clean_phone, business_name, city, review_count FROM leads
            WHERE (website_url IS NULL OR website_url = '')
              AND primary_status = 'Today'
        """)
        leads = cursor.fetchall()

    count = 0
    for lead in leads:
        lead_id = lead["id"]
        business_name = lead.get("business_name") or ""
        city = lead.get("city") or ""
        review_count = lead.get("review_count") or 0
        phone = lead.get("clean_phone")

        # Attempt to recover missing website for established businesses
        if review_count >= 5:
            recovered_url = recover_missing_website(business_name, city)
            if recovered_url:
                netloc = urllib.parse.urlparse(recovered_url).netloc.lower()
                clean_domain = netloc[4:] if netloc.startswith("www.") else netloc
                with database.get_connection(db_path) as conn:
                    conn.cursor().execute(
                        "UPDATE leads SET website_url = ?, clean_domain = ? WHERE id = ?",
                        (recovered_url, clean_domain, lead_id)
                    )
                    conn.commit()
                logging.info(f"Recovered unlisted website for '{business_name}': {recovered_url}")
                continue  # Skip marking as No Website; queued for normal audit

        flaw = {"failed_major_test": 1, "primary_flaw_label": "No Website Present"}
        primary_status, sub_status = determine_lead_status(
            has_major_flaw=True,
            is_unreachable=False,
            clean_phone=phone,
            dm_email=None,
            dm_linkedin=None,
            generic_email=None
        )
        database.update_lead_audit(lead_id, flaw, primary_status, sub_status, db_path=db_path)
        count += 1

    if count > 0:
        logging.info(f"Classified {count} Path B leads (No Website).")
    return count


async def run_site_auditor(config) -> None:
    """Main executor to run parallel site audits on all pending leads."""
    if isinstance(config, str):
        with open(config, "r", encoding="utf-8") as f:
            config = json.load(f)

    database.init_db(config["app"]["db_path"])

    # 1. Process Path B leads (No Website)
    process_no_website_leads(config)

    # 2. Process Path A leads (Has Website)
    leads_to_audit = database.get_unaudited_leads(config["app"]["db_path"])
    if not leads_to_audit:
        logging.info("No websites pending audit.")
        return

    concurrency = config["auditor"].get("concurrency_limit", 5)
    logging.info(f"Found {len(leads_to_audit)} websites to audit. Concurrency limit: {concurrency}")
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=config["auditor"].get("headless_args", ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
        )
        tasks = [audit_single_site(browser, lead, config, semaphore) for lead in leads_to_audit]
        await asyncio.gather(*tasks)
        await browser.close()

    logging.info("--- Site Audits & Routing Pass Complete ---")


# Alias for pipeline executor
run_audit_pipeline = run_site_auditor


if __name__ == "__main__":
    asyncio.run(run_site_auditor("config.json"))
