import asyncio
import json
import logging
import random
import re
import urllib.parse

from playwright.async_api import async_playwright, Page
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def clean_website_url(raw_url):
    """Strips Google redirect wrappers and extracts canonical URL and root domain."""
    if not raw_url:
        return None, None

    # Handle Google redirect wrapper: /url?q=https://example.com/&sa=...
    if "google.com/url" in raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        if "q" in query_params:
            raw_url = query_params["q"][0]

    raw_url = raw_url.strip()
    if not raw_url.startswith(("http://", "https://")):
        raw_url = f"https://{raw_url}"

    try:
        parsed = urllib.parse.urlparse(raw_url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        clean_domain = netloc.split(":")[0]
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        return clean_url, clean_domain
    except Exception:
        return raw_url, None


def clean_phone_number(raw_phone):
    """Normalizes phone string into E.164-like digits with leading plus."""
    if not raw_phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw_phone.strip())
    if len(cleaned) < 7:
        return None
    if len(cleaned) == 10 and not cleaned.startswith("+"):
        cleaned = f"+1{cleaned}"
    elif len(cleaned) == 11 and cleaned.startswith("1"):
        cleaned = f"+{cleaned}"
    return cleaned


def parse_niche_and_city(query, default_country="US"):
    """Parses niche and city from query strings like 'commercial roofer Dallas TX'."""
    query = query.strip()
    if " in " in query.lower():
        parts = re.split(r"\s+in\s+", query, flags=re.IGNORECASE, maxsplit=1)
        return parts[0].strip(), parts[1].strip()

    m = re.match(r"^(.*?)\s+([A-Z][a-zA-Z\s.-]+(?:\s+[A-Za-z]{2})?)$", query)
    if m:
        niche = m.group(1).strip()
        city = m.group(2).strip()
        if niche and city:
            return niche, city

    return query, ""


def extract_place_id(maps_url):
    """Extracts unique Google Maps Place/Feature ID from canonical URL."""
    if not maps_url:
        return None
    match = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", maps_url)
    if match:
        return match.group(1)
    match_fallback = re.search(r"(0x[0-9a-fA-F]{12,})", maps_url)
    return match_fallback.group(1) if match_fallback else maps_url[:100]


async def handle_consent(page: Page, selectors: dict) -> None:
    """Detects and accepts Google cookie / consent dialogs if present."""
    for btn_selector in selectors.get("consent_accept_buttons", []):
        try:
            button = page.locator(btn_selector).first
            if await button.is_visible(timeout=1500):
                await button.click()
                await page.wait_for_timeout(1000)
                logging.info("Dismissed Google consent dialog.")
                break
        except Exception:
            continue


async def scrape_single_query(page: Page, query: str, config: dict) -> int:
    """
    Executes a single Maps search query, scrolls the virtualized feed,
    deduplicates against SQLite, and stages new leads into 'Today'.
    """
    scraper_cfg = config["scraper_c1"]
    selectors = scraper_cfg["selectors"]
    db_path = config["app"]["db_path"]
    target_country = config["app"]["target_country"]
    lead_origin = scraper_cfg["lead_origin"]
    scroll_px = scraper_cfg["scroll_amount_px"]

    encoded_query = urllib.parse.quote_plus(query)
    search_url = f"https://www.google.com/maps/search/{encoded_query}?hl=en"

    logging.info(f"Opening Maps query: '{query}'")
    try:
        await page.goto(search_url, timeout=scraper_cfg["navigation_timeout_ms"], wait_until="domcontentloaded")
    except Exception as e:
        logging.error(f"Navigation failed for query '{query}': {e}")
        return 0

    await handle_consent(page, selectors)

    feed_selector = selectors["feed_container"]
    try:
        await page.wait_for_selector(feed_selector, timeout=10000)
    except Exception:
        logging.warning(f"No feed container found for '{query}'. Might have 0 results or direct business view.")
        return 0

    seen_card_keys = set()
    consecutive_empty_scrolls = 0
    total_saved_for_query = 0
    max_results = scraper_cfg["max_results_per_query"]
    max_empty_scrolls = scraper_cfg["max_consecutive_empty_scrolls"]

    while total_saved_for_query < max_results and consecutive_empty_scrolls < max_empty_scrolls:
        cards = await page.locator(selectors["listing_card"]).all()
        new_cards_found_in_pass = 0

        for card in cards:
            if total_saved_for_query >= max_results:
                break

            try:
                # Extract Maps URL and Place ID
                maps_url = await card.get_attribute("href")
                if not maps_url:
                    anchor = card.locator("a[href*='/maps/place/']").first
                    if await anchor.count() > 0:
                        maps_url = await anchor.get_attribute("href")

                if not maps_url or maps_url in seen_card_keys:
                    continue

                seen_card_keys.add(maps_url)
                place_id = extract_place_id(maps_url)

                # Extract Business Name
                name_elem = card.locator(selectors["business_name"]).first
                business_name = None
                if await name_elem.count() > 0:
                    business_name = (await name_elem.inner_text()).strip()
                if not business_name:
                    business_name = await card.get_attribute("aria-label")

                if not business_name:
                    continue

                # Extract Rating and Review Count
                rating = 0.0
                review_count = 0
                rating_elem = card.locator(selectors["rating_aria"]).first
                if await rating_elem.count() > 0:
                    aria_text = (await rating_elem.get_attribute("aria-label")) or ""
                    r_match = re.search(r"([0-9.]+)\s*star", aria_text)
                    if r_match:
                        try:
                            rating = float(r_match.group(1))
                        except ValueError:
                            pass

                review_elem = card.locator(selectors["review_count"]).first
                if await review_elem.count() > 0:
                    rev_text = (await review_elem.get_attribute("aria-label")) or (await review_elem.inner_text())
                    rev_match = re.search(r"([0-9,]+)", rev_text)
                    if rev_match:
                        review_count = int(rev_match.group(1).replace(",", ""))

                # Tier 1: Extract Website & Phone directly from feed card (if present)
                website_url = None
                clean_domain = None
                web_btn = card.locator(selectors["website_button"]).first
                if await web_btn.count() > 0:
                    raw_href = await web_btn.get_attribute("href")
                    website_url, clean_domain = clean_website_url(raw_href)

                phone = None
                phone_btn = card.locator(selectors["phone_button"]).first
                if await phone_btn.count() > 0:
                    phone = (await phone_btn.inner_text()).strip() or await phone_btn.get_attribute("data-tooltip")
                if not phone:
                    card_text = await card.inner_text()
                    phone_match = re.search(r"(\+?[0-9\s\-\(\)]{8,18}[0-9])", card_text)
                    if phone_match:
                        phone = phone_match.group(1)

                # Tier 2: Automatic Detail Pane Fallback (for medical, legal, and compact card layouts)
                if not website_url or not phone:
                    click_target = card.locator(selectors.get("card_click_anchor", "a.hfpxzc")).first
                    if await click_target.count() > 0:
                        try:
                            current_h1 = ""
                            title_loc = page.locator(selectors.get("detail_pane_title", "h1.DUwDvf"))
                            if await title_loc.count() > 0:
                                current_h1 = (await title_loc.first.inner_text()).strip()

                            await click_target.click()

                            # Wait for detail pane title to change and match current business name
                            try:
                                clean_expected = re.sub(r"[^a-zA-Z0-9]", "", business_name).lower()[:8]
                                await page.wait_for_function(
                                    """({oldName, exp}) => {
                                        const el = document.querySelector('h1.DUwDvf');
                                        if (!el) return false;
                                        const txt = el.textContent.trim();
                                        const cleanTxt = txt.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
                                        if (txt === oldName) return false;
                                        return cleanTxt.includes(exp) || exp.includes(cleanTxt.slice(0, 6));
                                    }""",
                                    {"oldName": current_h1, "exp": clean_expected},
                                    timeout=scraper_cfg.get("detail_pane_timeout_ms", 2500)
                                )
                            except Exception:
                                await page.wait_for_timeout(600)

                            if not website_url:
                                authority_sel = selectors.get("detail_pane_website", "a[data-item-id='authority']")
                                authority_btn = page.locator(authority_sel).first
                                if await authority_btn.count() > 0:
                                    raw_href = await authority_btn.get_attribute("href")
                                    website_url, clean_domain = clean_website_url(raw_href)

                            if not phone:
                                phone_sel = selectors.get("detail_pane_phone", "[data-item-id^='phone:tel:']")
                                phone_btn = page.locator(phone_sel).first
                                if await phone_btn.count() > 0:
                                    data_id = (await phone_btn.get_attribute("data-item-id")) or ""
                                    if "phone:tel:" in data_id:
                                        phone = data_id.split("phone:tel:")[-1].strip()
                                if not phone:
                                    p_loc = page.locator("button[aria-label*='Phone:' i]").first
                                    if await p_loc.count() > 0:
                                        aria = (await p_loc.get_attribute("aria-label")) or ""
                                        m = re.search(r"(\+?[0-9\s\-\(\)]{8,18}[0-9])", aria)
                                        if m:
                                            phone = m.group(1).strip()
                        except Exception as pane_err:
                            logging.debug(f"Detail pane extraction error for '{business_name}': {pane_err}")

                clean_phone = clean_phone_number(phone)

                # Deduplication Check against SQLite Master Archive
                if database.is_duplicate(clean_domain=clean_domain, maps_place_id=place_id, clean_phone=clean_phone, db_path=db_path):
                    logging.info(f"Duplicate skipped: '{business_name}' ({clean_domain or place_id})")
                    continue

                niche, city = parse_niche_and_city(query, target_country)

                # Stage New Record into SQLite
                lead_payload = {
                    "business_name": business_name,
                    "niche": niche,
                    "city": city,
                    "country": target_country,
                    "maps_place_id": place_id,
                    "maps_url": maps_url,
                    "rating": rating,
                    "review_count": review_count,
                    "clean_domain": clean_domain,
                    "website_url": website_url,
                    "phone": phone.strip() if phone else None,
                    "clean_phone": clean_phone,
                    "lead_origin_pipeline": lead_origin,
                    "search_query_raw": query,
                    "primary_status": "Today",
                    "sub_status": "Pending Evaluation"
                }

                database.insert_lead(lead_payload, db_path=db_path)
                total_saved_for_query += 1
                new_cards_found_in_pass += 1
                logging.info(f"[+] Saved: '{business_name}' | Site: {clean_domain} | Phone: {clean_phone}")

            except Exception as item_err:
                logging.debug(f"Card parse error: {item_err}")
                continue

        # Check for Google's "End of list" marker
        end_marker = page.locator(selectors["end_of_list_marker"]).first
        if await end_marker.count() > 0 and await end_marker.is_visible():
            logging.info(f"Reached end of list for query: '{query}'")
            break

        if new_cards_found_in_pass > 0:
            consecutive_empty_scrolls = 0
        else:
            consecutive_empty_scrolls += 1

        # Humanized randomized scroll down inside div[role="feed"]
        await page.evaluate("""(args) => {
            const el = document.querySelector(args.sel);
            if (el) el.scrollBy(0, args.px);
        }""", {"sel": feed_selector, "px": scroll_px})
        await page.wait_for_timeout(random.randint(scraper_cfg["scroll_delay_min_ms"], scraper_cfg["scroll_delay_max_ms"]))

    logging.info(f"Completed query '{query}'. Total new leads staged: {total_saved_for_query}")
    return total_saved_for_query


async def run_maps_scraper(config) -> int:
    """Main executor for c1 Google Maps scraper. Accepts config dict or path string."""
    if isinstance(config, str):
        with open(config, "r", encoding="utf-8") as f:
            config = json.load(f)

    database.init_db(config["app"]["db_path"])

    scraper_cfg = config["scraper_c1"]
    total_harvested = 0

    async with async_playwright() as p:
        logging.info("Launching Chrome Persistent Context for Maps extraction...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=scraper_cfg["user_data_dir"],
            channel=scraper_cfg.get("browser_channel", "chrome"),
            headless=scraper_cfg.get("headless", False),
            args=scraper_cfg.get("launch_args", []),
            viewport=scraper_cfg.get("viewport", {"width": 1920, "height": 1080})
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # Stealth CDP override
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        for query in scraper_cfg.get("target_queries", []):
            count = await scrape_single_query(page, query, config)
            total_harvested += count
            # Query cooldown jitter
            await page.wait_for_timeout(random.randint(
                scraper_cfg["query_cooldown_min_ms"],
                scraper_cfg["query_cooldown_max_ms"]
            ))

        await context.close()

    logging.info(f"--- c1 Scraper Run Complete. Total Staged Today: {total_harvested} ---")
    return total_harvested


if __name__ == "__main__":
    asyncio.run(run_maps_scraper("config.json"))
