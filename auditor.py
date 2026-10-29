import asyncio
import json
import logging
import re
import urllib.parse
from typing import Dict, Any, List, Tuple, Optional

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
    Returns: (dm_or_direct_emails, generic_emails)
    """
    raw_matches = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html_content)
    dm_emails = []
    generic_emails = []
    seen = set()
    invalid_suffixes = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js")

    for email in raw_matches:
        email_clean = email.lower().strip().rstrip(".,;:)")
        if not email_clean or email_clean in seen or any(email_clean.endswith(ext) for ext in invalid_suffixes):
            continue
        if "sentry" in email_clean or "wixpress" in email_clean or "example.com" in email_clean:
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


def extract_decision_maker(html_content: str, keywords: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts Decision Maker name and title from JSON-LD schema or page text heuristics.
    Returns: (dm_name, dm_title)
    """
    if not keywords:
        return None, None

    # 1. Inspect Schema.org JSON-LD scripts
    try:
        ld_scripts = re.findall(r'<script[^>]*type=["\'"]application/ld\+json["\'][^>]*>(.*?)</script>', html_content, re.DOTALL | re.IGNORECASE)
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

    # 2. Text heuristics against decision maker keywords
    text_clean = re.sub(r"<script.*?</script>", " ", html_content, flags=re.DOTALL | re.IGNORECASE)
    text_clean = re.sub(r"<style.*?</style>", " ", text_clean, flags=re.DOTALL | re.IGNORECASE)
    text_clean = re.sub(r"<[^>]+>", " \n ", text_clean)
    lines = [line.strip() for line in text_clean.splitlines() if line.strip()]

    kw_pattern = "|".join(re.escape(k) for k in keywords)

    for line in lines:
        if len(line) > 100:
            continue
        m1 = re.search(rf"^([A-Z][a-z]+(?:\s+[A-Z]\.?)*(?:\s+[A-Z][a-z]+)+)[,\s\-–|]+(?:the\s+)?({kw_pattern})\b", line, re.IGNORECASE)
        if m1:
            return m1.group(1).strip(), m1.group(2).strip().title()
        m2 = re.search(rf"^(?:the\s+)?({kw_pattern})[:\s\-–|]+([A-Z][a-z]+(?:\s+[A-Z]\.?)*(?:\s+[A-Z][a-z]+)+)$", line, re.IGNORECASE)
        if m2:
            return m2.group(2).strip(), m2.group(1).strip().title()

    return None, None


async def audit_single_site(
    browser: Browser,
    lead: Dict[str, Any],
    config: Dict[str, Any],
    semaphore: asyncio.Semaphore
) -> None:
    """Audits speed, mobile responsiveness, bloat, and contacts for a single website."""
    auditor_cfg = config["auditor"]
    thresholds = auditor_cfg["thresholds"]
    generic_prefixes = auditor_cfg["generic_email_prefixes"]
    dm_keywords = auditor_cfg.get("decision_maker_keywords", [])
    db_path = config["app"]["db_path"]

    lead_id = lead["id"]
    url = lead["website_url"]
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    total_bytes = 0

    async with semaphore:
        logging.info(f"Auditing [{lead_id}]: {url}")
        context = await browser.new_context(
            viewport=auditor_cfg["mobile_viewport"],
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
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
            await page.goto(url, timeout=auditor_cfg["page_timeout_ms"], wait_until="load")
            await page.wait_for_timeout(1000)

            metrics = await page.evaluate("""() => {
                const nav = performance.getEntriesByType('navigation')[0];
                let loadTimeSec = 0;
                let ttfbMs = 0;
                if (nav) {
                    loadTimeSec = (nav.loadEventEnd > 0 ? (nav.loadEventEnd - nav.startTime) : nav.duration) / 1000;
                    ttfbMs = Math.round(nav.responseStart - nav.requestStart);
                }
                const domNodes = document.querySelectorAll('*').length;
                const mobileBroken = document.documentElement.scrollWidth > (window.innerWidth + 5);
                return {
                    load_time_sec: Math.max(0, loadTimeSec),
                    ttfb_ms: Math.max(0, ttfbMs),
                    dom_nodes: domNodes,
                    mobile_broken: mobileBroken ? 1 : 0
                };
            }""")

            html_content = await page.content()
            cms = detect_cms(html_content)
            dm_emails, generic_emails = extract_emails(html_content, generic_prefixes)
            dm_linkedin = extract_linkedin(html_content)
            dm_name, dm_title = extract_decision_maker(html_content, dm_keywords)

            # Secondary shallow crawl: check /contact or /about if contacts are missing
            if (not dm_emails and not generic_emails) or not dm_name:
                try:
                    contact_loc = page.locator("a[href*='contact'], a[href*='about']").first
                    if await contact_loc.count() > 0:
                        contact_link = await contact_loc.get_attribute("href", timeout=2000)
                        if contact_link:
                            contact_full_url = urllib.parse.urljoin(url, contact_link)
                            if urllib.parse.urlparse(contact_full_url).netloc == urllib.parse.urlparse(url).netloc:
                                await page.goto(contact_full_url, timeout=10000, wait_until="domcontentloaded")
                                contact_html = await page.content()
                                c_dm, c_gen = extract_emails(contact_html, generic_prefixes)
                                dm_emails.extend(c_dm)
                                generic_emails.extend(c_gen)
                                if not dm_linkedin:
                                    dm_linkedin = extract_linkedin(contact_html)
                                if not dm_name:
                                    dm_name, dm_title = extract_decision_maker(contact_html, dm_keywords)
                except Exception:
                    pass

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

            failed_major_test = 1 if failures else 0
            primary_flaw_label = ", ".join(failures) if failures else "None"

            chosen_dm_email = dm_emails[0] if dm_emails else None
            chosen_generic_email = generic_emails[0] if generic_emails else None

            # Qualification: DM email or LinkedIn required for Outreach Ready
            if not failed_major_test:
                primary_status = "Disqualified"
                sub_status = "Healthy Site"
            elif chosen_dm_email or dm_linkedin:
                primary_status = "Outreach Ready"
                sub_status = "Uncontacted"
            else:
                primary_status = "Missing Info"
                sub_status = "Needs DM Email" if chosen_generic_email else "Needs DM Contact"

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
                "generic_email": chosen_generic_email
            }

            database.update_lead_audit(lead_id, audit_payload, primary_status, sub_status, db_path=db_path)
            logging.info(f"[Audit Completed] ID {lead_id} -> {primary_status} ({sub_status}) | Flaws: {primary_flaw_label}")

        except Exception as e:
            logging.warning(f"Audit failed for ID {lead_id} ({url}): {e}")
            database.update_lead_audit(
                lead_id,
                {"failed_major_test": 1, "primary_flaw_label": f"Site Unreachable: {str(e)[:50]}"},
                "Disqualified",
                "Dead Domain / Unreachable",
                db_path=db_path
            )
        finally:
            await context.close()


def process_no_website_leads(config: Dict[str, Any]) -> int:
    """Classifies Path B leads (no website) directly without browser audit."""
    db_path = config["app"]["db_path"]
    with database.get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, clean_phone FROM leads
            WHERE (website_url IS NULL OR website_url = '')
              AND primary_status = 'Today'
        """)
        no_web_leads = cursor.fetchall()

    count = 0
    for row in no_web_leads:
        lead_id = row["id"]
        phone = row["clean_phone"]
        flaw = {"failed_major_test": 1, "primary_flaw_label": "No Website Present"}
        if phone:
            database.update_lead_audit(lead_id, flaw, "Outreach Ready", "Uncontacted (No Website)", db_path=db_path)
        else:
            database.update_lead_audit(lead_id, flaw, "Missing Info", "Needs Contact (No Website)", db_path=db_path)
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

    logging.info(f"Found {len(leads_to_audit)} websites to audit. Concurrency limit: {config['auditor']['concurrency_limit']}")
    semaphore = asyncio.Semaphore(config["auditor"]["concurrency_limit"])

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=config["auditor"]["headless_args"]
        )
        tasks = [audit_single_site(browser, lead, config, semaphore) for lead in leads_to_audit]
        await asyncio.gather(*tasks)
        await browser.close()

    logging.info("--- Site Audits & Routing Pass Complete ---")


if __name__ == "__main__":
    asyncio.run(run_site_auditor("config.json"))
