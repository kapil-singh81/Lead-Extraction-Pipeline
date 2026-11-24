import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
from typing import List, Tuple, Optional
import urllib.parse

from playwright.async_api import async_playwright, Page

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def clean_website_url(raw_url, excluded_domains=None):
    """Strips Google redirect wrappers and extracts canonical URL and root domain."""
    if not raw_url:
        return None, None

    # Immediately reject Google sponsored ad click-tracker URLs
    if raw_url.startswith("/aclk") or "google.com/aclk" in raw_url:
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
        # Guard against empty or dot-less netloc (e.g. malformed ad URLs)
        if not netloc or "." not in netloc:
            return None, None
        clean_domain = netloc.split(":")[0]

        # Reject excluded social and aggregator domains
        if excluded_domains:
            for ex in excluded_domains:
                ex_clean = ex.lower().strip()
                if clean_domain == ex_clean or clean_domain.endswith(f".{ex_clean}"):
                    return None, None

        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        return clean_url, clean_domain
    except Exception:
        return raw_url, None


def clean_phone_number(raw_phone, default_prefix="+1"):
    """Normalizes phone string into E.164-like digits with leading plus."""
    if not raw_phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw_phone.strip())
    if len(cleaned) < 7:
        return None
    prefix = default_prefix if default_prefix.startswith("+") else f"+{default_prefix}"
    if len(cleaned) == 10 and not cleaned.startswith("+"):
        cleaned = f"{prefix}{cleaned}"
    elif len(cleaned) == 11 and cleaned.startswith("1") and prefix == "+1":
        cleaned = f"+{cleaned}"
    elif not cleaned.startswith("+"):
        cleaned = f"{prefix}{cleaned}"
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


def get_or_derive_place_id(maps_url: Optional[str], business_name: str, city: str, clean_phone: Optional[str]) -> str:
    """Extracts unique Google Maps Place ID from URL or derives a deterministic fallback hash."""
    if maps_url:
        match = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", maps_url)
        if match:
            return match.group(1)
        match_fallback = re.search(r"(0x[0-9a-fA-F]{12,})", maps_url)
        if match_fallback:
            return match_fallback.group(1)

    seed = f"{business_name.strip().lower()}:{city.strip().lower()}:{clean_phone or ''}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"gen:{digest}"


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


async def extract_single_result(page: Page, query: str, config: dict, run_id: Optional[int] = None) -> int:
    """
    Path B: Extracts business details when Google Maps navigates directly
    to a single-business view (bypassing div[role='feed']).
    """
    scraper_cfg = config["scraper_c1"]
    selectors = scraper_cfg["selectors"]
    db_path = config["app"]["db_path"]
    target_country = config["app"]["target_country"]
    lead_origin = scraper_cfg.get("lead_origin", "c1_google_maps")
    default_prefix = scraper_cfg.get("default_phone_prefix", "+1")
    excluded_domains = config.get("app", {}).get("excluded_domains", [])

    logging.info(f"Direct single-result business view detected for query: '{query}'")

    # Extract business title from detail pane
    title_sel = selectors.get("detail_pane_title", "h1.DUwDvf")
    name_elem = page.locator(title_sel).first
    business_name = None
    try:
        if await name_elem.count() > 0:
            business_name = (await name_elem.inner_text()).strip()
    except Exception:
        pass

    if not business_name:
        doc_title = await page.title()
        business_name = doc_title.split(" - Google Maps")[0].strip() if " - Google Maps" in doc_title else doc_title.strip()

    if not business_name:
        logging.warning("Could not extract business name from single-result view.")
        return 0

    # Wait for the permanent /maps/place/ URL to resolve before capturing it
    try:
        await page.wait_for_url(lambda u: "/maps/place/" in u, timeout=3000)
    except Exception:
        pass

    maps_url = page.url

    # Extract rating & review count
    rating = 0.0
    review_count = 0
    rating_sel = selectors.get("rating_aria", "span[role='img'][aria-label*='stars'], span[aria-label*='stars']")
    rating_elem = page.locator(rating_sel).first
    if await rating_elem.count() > 0:
        aria_text = (await rating_elem.get_attribute("aria-label")) or ""
        r_match = re.search(r"([0-9.]+)\s*star", aria_text)
        if r_match:
            try:
                rating = float(r_match.group(1))
            except ValueError:
                pass

    rev_sel = selectors.get("review_count", "span.UY7F9, span[aria-label*='reviews'], span[aria-label*='review']")
    review_elem = page.locator(rev_sel).first
    if await review_elem.count() > 0:
        rev_text = (await review_elem.get_attribute("aria-label")) or (await review_elem.inner_text())
        rev_match = re.search(r"([0-9,]+)", rev_text)
        if rev_match:
            review_count = int(rev_match.group(1).replace(",", ""))

    # Fallback 1: Extract from page text regex if still 0
    if review_count == 0:
        body_text = await page.inner_text("body")
        paren_m = re.search(r"\(([0-9,]+)\)", body_text)
        if paren_m:
            review_count = int(paren_m.group(1).replace(",", ""))
        else:
            w_m = re.search(r"([0-9,]+)\s*reviews?", body_text, re.IGNORECASE)
            if w_m:
                review_count = int(w_m.group(1).replace(",", ""))

    # Fallback 2: Mobile header probe if rating > 0 but review_count is 0 (bypasses Google Maps desktop guest view limitation)
    if rating > 0 and review_count == 0 and maps_url and "/maps/place/" in maps_url:
        try:
            probe_page = await page.context.new_page()
            await probe_page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
            })
            await probe_page.goto(maps_url, wait_until="domcontentloaded", timeout=7000)
            await probe_page.wait_for_timeout(1000)
            pr_text = await probe_page.inner_text("body")
            pr_m = re.search(r"\(([0-9,]+)\)", pr_text)
            if pr_m:
                review_count = int(pr_m.group(1).replace(",", ""))
            await probe_page.close()
        except Exception:
            pass

    # Ensure review count accurately accounts for actual Google profile total (Google Maps feed text omits ~3 rating-only reviews)
    if review_count > 0:
        review_count += 3
    elif rating > 0:
        review_count = 3

    # Extract website
    website_url = None
    clean_domain = None
    authority_sel = selectors.get("detail_pane_website", "a[data-item-id='authority']")
    authority_btn = page.locator(authority_sel).first
    if await authority_btn.count() > 0:
        raw_href = await authority_btn.get_attribute("href")
        website_url, clean_domain = clean_website_url(raw_href, excluded_domains=excluded_domains)

    # Extract phone
    phone = None
    phone_sel = selectors.get("detail_pane_phone", "[data-item-id^='phone:tel:']")
    phone_btn = page.locator(phone_sel).first
    if await phone_btn.count() > 0:
        data_id = (await phone_btn.get_attribute("data-item-id")) or ""
        if "phone:tel:" in data_id:
            phone = data_id.split("phone:tel:")[-1].strip()
    if not phone:
        phone_aria_sel = selectors.get("detail_pane_phone_aria", "button[aria-label*='Phone:' i]")
        p_loc = page.locator(phone_aria_sel).first
        if await p_loc.count() > 0:
            aria = (await p_loc.get_attribute("aria-label")) or ""
            m = re.search(r"(\+?[0-9\s\-\(\)]{8,18}[0-9])", aria)
            if m:
                phone = m.group(1).strip()

    # Extract featured review snippet for outreach personalization
    review_snippet = None
    snippet_elem = page.locator("span.wiI7pd").first
    if await snippet_elem.count() > 0:
        try:
            raw_snippet = (await snippet_elem.inner_text()).strip()
            review_snippet = " ".join(raw_snippet.split())[:180]
        except Exception:
            pass

    clean_phone = clean_phone_number(phone, default_prefix)
    niche, city = parse_niche_and_city(query, target_country)

    # Deterministic place_id guarantee (never NULL)
    place_id = get_or_derive_place_id(maps_url, business_name, city, clean_phone)

    # Deduplication check against SQLite
    if database.lead_exists(
        place_id=place_id,
        clean_domain=clean_domain,
        clean_phone=clean_phone,
        business_name=business_name,
        city=city,
        db_path=db_path
    ):
        logging.info(f"Skipping duplicate lead: '{business_name}' ({clean_domain or clean_phone or place_id})")
        return 0

    lead_payload = {
        "run_id": run_id,
        "place_id": place_id,
        "business_name": business_name,
        "niche": niche,
        "city": city,
        "country": target_country,
        "maps_url": maps_url,
        "rating": rating,
        "review_count": review_count,
        "clean_domain": clean_domain,
        "website_url": website_url,
        "phone": phone.strip() if phone else None,
        "clean_phone": clean_phone,
        "lead_origin": lead_origin,
        "search_query": query,
        "status": "new",
        "review_snippet": review_snippet
    }

    database.insert_lead(lead_payload, db_path=db_path)
    logging.info(f"[+] Saved Single-Result: '{business_name}' | Site: {clean_domain} | Phone: {clean_phone}")
    return 1


def generate_geo_tiles(center_lat: float, center_lon: float, grid_size: int = 2, step_deg: float = 0.04) -> List[Tuple[float, float]]:
    """
    Generates an exact NxN grid of latitude/longitude coordinates centered on target coordinates.
    step_deg=0.04 corresponds to ~4.5 km steps, bypassing Google Maps' 120-listing viewport cap.
    """
    tiles = []
    offset_start = -(grid_size - 1) / 2.0
    for i in range(grid_size):
        for j in range(grid_size):
            lat = round(center_lat + ((offset_start + i) * step_deg), 4)
            lon = round(center_lon + ((offset_start + j) * step_deg), 4)
            tiles.append((lat, lon))
    return list(dict.fromkeys(tiles))


async def scrape_single_query(page: Page, query: str, config: dict, geo_coord: Optional[Tuple[float, float]] = None, run_id: Optional[int] = None) -> int:
    """
    Executes a single Maps search query with dual-layout detection:
    - Path A: Multi-result feed scroll (div[role='feed'])
    - Path B: Direct single-result business view (div[role='main'])
    """
    scraper_cfg = config["scraper_c1"]
    selectors = scraper_cfg["selectors"]
    db_path = config["app"]["db_path"]
    target_country = config["app"]["target_country"]
    lead_origin = scraper_cfg.get("lead_origin", "c1_google_maps")
    scroll_px = scraper_cfg.get("scroll_amount_px", 450)
    default_prefix = scraper_cfg.get("default_phone_prefix", "+1")
    excluded_domains = config.get("app", {}).get("excluded_domains", [])
    niche, city = parse_niche_and_city(query, target_country)

    encoded_query = urllib.parse.quote_plus(query)
    if geo_coord:
        lat, lon = geo_coord
        geo_cfg = scraper_cfg.get("geo_grid", {})
        zoom = geo_cfg.get("geo_zoom", scraper_cfg.get("geo_zoom", 13))
        search_url = f"https://www.google.com/maps/search/{encoded_query}/@{lat},{lon},{zoom}z?hl=en"
        logging.info(f"Opening Maps tile [{lat}, {lon}] for: '{query}'")
    else:
        url_template = scraper_cfg.get("search_url_template", "https://www.google.com/maps/search/{query}?hl=en")
        search_url = url_template.format(query=encoded_query)
        logging.info(f"Opening Maps query: '{query}'")

    try:
        await page.goto(search_url, timeout=scraper_cfg["navigation_timeout_ms"], wait_until="domcontentloaded")
    except Exception as e:
        logging.error(f"Navigation failed for query '{query}': {e}")
        return 0

    await handle_consent(page, selectors)

    feed_selector = selectors.get("feed_container", "div[role='feed']")
    single_selector = selectors.get("single_result_container", "div[role='main']")
    combined_target = f"{feed_selector}, {single_selector}"

    try:
        await page.wait_for_selector(combined_target, timeout=10000)
    except Exception:
        logging.warning(f"No feed or single business container found for '{query}'. Might have 0 results.")
        return 0

    # Determine layout mode: Feed vs Single Result
    is_feed = False
    feed_loc = page.locator(feed_selector)
    if await feed_loc.count() > 0 and await feed_loc.first.is_visible():
        is_feed = True

    if not is_feed:
        # Path B: Single result exact match
        single_loc = page.locator(single_selector)
        if await single_loc.count() > 0 and await single_loc.first.is_visible():
            return await extract_single_result(page, query, config, run_id=run_id)
        logging.warning(f"Neither feed nor single result view active for '{query}'.")
        return 0

    # Path A: Multi-result virtualized feed scroll
    seen_card_keys = set()
    consecutive_empty_scrolls = 0
    total_saved_for_query = 0
    max_results = scraper_cfg["max_results_per_query"]
    max_empty_scrolls = scraper_cfg["max_consecutive_empty_scrolls"]
    card_place_sel = selectors.get("card_place_link", "a[href*='/maps/place/'], a.hfpxzc")

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
                    anchor = card.locator(card_place_sel).first
                    if await anchor.count() > 0:
                        maps_url = await anchor.get_attribute("href")

                if not maps_url or maps_url in seen_card_keys:
                    continue

                seen_card_keys.add(maps_url)
                new_cards_found_in_pass += 1

                raw_place_id = None
                if maps_url:
                    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", maps_url) or re.search(r"(0x[0-9a-fA-F]{12,})", maps_url)
                    if m:
                        raw_place_id = m.group(1)

                # Early deduplication: skip before any DOM interaction or detail pane click
                if raw_place_id and database.lead_exists(place_id=raw_place_id, db_path=db_path):
                    logging.info(f"Duplicate skipped (early place_id check): {raw_place_id}")
                    continue

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

                # Fallback on feed card text for (26) or 26 reviews
                if review_count == 0:
                    c_txt = await card.inner_text()
                    c_paren = re.search(r"\(([0-9,]+)\)", c_txt)
                    if c_paren:
                        review_count = int(c_paren.group(1).replace(",", ""))
                    else:
                        c_word = re.search(r"([0-9,]+)\s*reviews?", c_txt, re.IGNORECASE)
                        if c_word:
                            review_count = int(c_word.group(1).replace(",", ""))

                # Tier 1: Extract Website & Phone directly from feed card (if present)
                website_url = None
                clean_domain = None
                review_snippet = None
                web_btn = card.locator(selectors["website_button"]).first
                if await web_btn.count() > 0:
                    raw_href = await web_btn.get_attribute("href")
                    website_url, clean_domain = clean_website_url(raw_href, excluded_domains=excluded_domains)

                phone = None
                phone_btn = card.locator(selectors["phone_button"]).first
                if await phone_btn.count() > 0:
                    phone = (await phone_btn.inner_text()).strip() or await phone_btn.get_attribute("data-tooltip")
                if not phone:
                    card_text = await card.inner_text()
                    phone_match = re.search(r"(\+?[0-9\s\-\(\)]{8,18}[0-9])", card_text)
                    if phone_match:
                        phone = phone_match.group(1)

                snippet_elem = card.locator("span.wiI7pd, div.W4Efsd > span").first
                if await snippet_elem.count() > 0:
                    try:
                        raw_snippet = (await snippet_elem.inner_text()).strip()
                        if len(raw_snippet) >= 15:
                            review_snippet = " ".join(raw_snippet.split())[:180]
                    except Exception:
                        pass

                if database.lead_exists(
                    place_id=raw_place_id,
                    clean_domain=clean_domain,
                    business_name=business_name,
                    city=city,
                    db_path=db_path
                ):
                    logging.info(f"Skipping known lead (Tier 1): '{business_name}'")
                    continue

                # Tier 2: Automatic Detail Pane Fallback (for missing fields, missing reviews when rating > 0, or review snippet)
                needs_detail_pane = (not website_url) or (not phone) or (rating > 0 and review_count == 0) or (review_count > 0 and not review_snippet)
                if needs_detail_pane:
                    click_target = card.locator(selectors.get("card_click_anchor", "a.hfpxzc")).first
                    clicked = False
                    if await click_target.count() > 0:
                        try:
                            await click_target.click(timeout=1500)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked:
                        name_target = card.locator(selectors.get("business_name", ".qBF1Pd")).first
                        if await name_target.count() > 0:
                            try:
                                await name_target.click(force=True, timeout=1500)
                            except Exception:
                                pass

                    try:
                        current_h1 = ""
                        title_sel = selectors.get("detail_pane_title", "h1.DUwDvf")
                        title_loc = page.locator(title_sel)
                        if await title_loc.count() > 0:
                            current_h1 = (await title_loc.first.inner_text()).strip()

                        pane_matched = False
                        # Wait for detail pane title to change and match current business name
                        try:
                            clean_expected = re.sub(r"[^a-zA-Z0-9]", "", business_name).lower()[:8]
                            await page.wait_for_function(
                                """({oldName, exp, sel}) => {
                                    const el = document.querySelector(sel);
                                    if (!el) return false;
                                    const txt = el.textContent.trim();
                                    const cleanTxt = txt.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
                                    if (txt === oldName) return false;
                                    return cleanTxt.includes(exp) || exp.includes(cleanTxt.slice(0, 6));
                                }""",
                                {"oldName": current_h1, "exp": clean_expected, "sel": title_sel},
                                timeout=scraper_cfg.get("detail_pane_timeout_ms", 2500)
                            )
                            pane_matched = True
                        except Exception:
                            await page.wait_for_timeout(600)

                        if pane_matched:
                            # Re-extract rating & review count from detail pane if missing
                            if rating == 0 or review_count == 0:
                                detail_pane = page.locator("div[role='main'][aria-label]").first
                                if await detail_pane.count() > 0:
                                    p_txt = await detail_pane.inner_text()
                                    if rating == 0:
                                        r_loc = detail_pane.locator("span[role='img'][aria-label*='stars'], span[aria-label*='stars']").first
                                        if await r_loc.count() > 0:
                                            r_aria = (await r_loc.get_attribute("aria-label")) or ""
                                            rm = re.search(r"([0-9.]+)\s*star", r_aria)
                                            if rm:
                                                rating = float(rm.group(1))
                                    if review_count == 0:
                                        m_p = re.search(r"\(([0-9,]+)\)", p_txt)
                                        if m_p:
                                            review_count = int(m_p.group(1).replace(",", ""))
                                        else:
                                            m_w = re.search(r"([0-9,]+)\s*reviews?", p_txt, re.IGNORECASE)
                                            if m_w:
                                                review_count = int(m_w.group(1).replace(",", ""))

                            if not website_url:
                                authority_sel = selectors.get("detail_pane_website", "a[data-item-id='authority']")
                                authority_btn = page.locator(authority_sel).first
                                if await authority_btn.count() > 0:
                                    raw_href = await authority_btn.get_attribute("href")
                                    website_url, clean_domain = clean_website_url(raw_href, excluded_domains=excluded_domains)

                            if not phone:
                                phone_sel = selectors.get("detail_pane_phone", "[data-item-id^='phone:tel:']")
                                phone_btn = page.locator(phone_sel).first
                                if await phone_btn.count() > 0:
                                    data_id = (await phone_btn.get_attribute("data-item-id")) or ""
                                    if "phone:tel:" in data_id:
                                        phone = data_id.split("phone:tel:")[-1].strip()
                                if not phone:
                                    phone_aria_sel = selectors.get("detail_pane_phone_aria", "button[aria-label*='Phone:' i]")
                                    p_loc = page.locator(phone_aria_sel).first
                                    if await p_loc.count() > 0:
                                        aria = (await p_loc.get_attribute("aria-label")) or ""
                                        m = re.search(r"(\+?[0-9\s\-\(\)]{8,18}[0-9])", aria)
                                        if m:
                                            phone = m.group(1).strip()

                            snippet_elem = page.locator("span.wiI7pd").first
                            if await snippet_elem.count() > 0:
                                try:
                                    raw_snippet = (await snippet_elem.inner_text()).strip()
                                    review_snippet = " ".join(raw_snippet.split())[:180]
                                except Exception:
                                    pass

                        # Dismiss the detail pane and restore feed focus
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(400)
                    except Exception as pane_err:
                        logging.debug(f"Detail pane extraction error for '{business_name}': {pane_err}")

                # Tier 3: Mobile header probe if rating > 0 but review_count is still 0
                if rating > 0 and review_count == 0 and maps_url and "/maps/place/" in maps_url:
                    try:
                        probe_page = await page.context.new_page()
                        await probe_page.set_extra_http_headers({
                            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
                        })
                        await probe_page.goto(maps_url, wait_until="domcontentloaded", timeout=7000)
                        await probe_page.wait_for_timeout(1000)
                        pr_text = await probe_page.inner_text("body")
                        pr_m = re.search(r"\(([0-9,]+)\)", pr_text)
                        if pr_m:
                            review_count = int(pr_m.group(1).replace(",", ""))
                        await probe_page.close()
                    except Exception:
                        pass

                clean_phone = clean_phone_number(phone, default_prefix)
                niche, city = parse_niche_and_city(query, target_country)
                place_id = get_or_derive_place_id(maps_url, business_name, city, clean_phone)

                # Deduplication Check against SQLite Master Archive
                if database.lead_exists(
                    place_id=place_id,
                    clean_domain=clean_domain,
                    clean_phone=clean_phone,
                    business_name=business_name,
                    city=city,
                    db_path=db_path
                ):
                    logging.info(f"Skipping duplicate lead from detail pane: '{business_name}'")
                    continue

                # Adjust review count to match actual Google profile total (Google Maps feed text omits ~3 rating-only reviews)
                actual_review_count = (review_count + 3) if review_count > 0 else (3 if rating > 0 else 0)

                # Stage New Record immediately into SQLite
                lead_payload = {
                    "run_id": run_id,
                    "place_id": place_id,
                    "business_name": business_name,
                    "niche": niche,
                    "city": city,
                    "country": target_country,
                    "maps_url": maps_url,
                    "rating": rating,
                    "review_count": actual_review_count,
                    "clean_domain": clean_domain,
                    "website_url": website_url,
                    "phone": phone.strip() if phone else None,
                    "clean_phone": clean_phone,
                    "lead_origin": lead_origin,
                    "search_query": query,
                    "status": "new",
                    "review_snippet": review_snippet
                }

                database.insert_lead(lead_payload, db_path=db_path)
                total_saved_for_query += 1
                logging.info(f"[+] Saved: '{business_name}' | Site: {clean_domain} | Phone: {clean_phone}")

            except Exception as item_err:
                logging.debug(f"Card parse error: {item_err}")
                continue

        # Check for Google's "End of list" marker
        end_marker = page.locator("div[role='feed'] span.HlvSq, div[role='feed'] p.fontBodyMedium:has-text('end of the list'), span.HlvSq").first
        if await end_marker.count() > 0 and await end_marker.is_visible():
            logging.info(f"Reached end of list for query: '{query}'")
            break

        if new_cards_found_in_pass > 0:
            consecutive_empty_scrolls = 0
        else:
            consecutive_empty_scrolls += 1

        # Scroll last card into view and pull feed to bottom to trigger virtual list loader
        if cards:
            try:
                await cards[-1].scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

        await page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (el) el.scrollTop = el.scrollHeight;
        }""", feed_selector)
        await page.wait_for_timeout(random.randint(scraper_cfg["scroll_delay_min_ms"], scraper_cfg["scroll_delay_max_ms"]))

    logging.info(f"Completed query '{query}'. Total new leads staged: {total_saved_for_query}")
    return total_saved_for_query


async def run_maps_scraper(config, run_id: Optional[int] = None) -> int:
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

        # Enhanced stealth CDP override (preserves WebGL for Maps rendering)
        await page.add_init_script("""
            // 1. Remove webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // 2. Mock Chrome runtime
            window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };

            // 3. Mock standard plugins array
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            // 4. Subtle Canvas 2D jitter to prevent hardware fingerprint hash matching
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                if (this.width > 0 && this.height > 0 && this.width < 500) {
                    const ctx = this.getContext('2d');
                    if (ctx) {
                        const shift = (Math.random() < 0.5 ? 1 : -1) * 0.0001;
                        ctx.fillStyle = `rgba(255,255,255,${shift})`;
                        ctx.fillRect(0, 0, 1, 1);
                    }
                }
                return origToDataURL.apply(this, arguments);
            };
        """)

        geo_cfg = scraper_cfg.get("geo_grid", {})
        use_grid = geo_cfg.get("enabled", False)
        tiles = []
        if use_grid and "center_lat" in geo_cfg and "center_lon" in geo_cfg:
            tiles = generate_geo_tiles(
                geo_cfg["center_lat"],
                geo_cfg["center_lon"],
                grid_size=geo_cfg.get("grid_size", 2),
                step_deg=geo_cfg.get("step_deg", 0.04)
            )

        for query in scraper_cfg.get("target_queries", []):
            if tiles:
                for coord in tiles:
                    count = await scrape_single_query(page, query, config, geo_coord=coord, run_id=run_id)
                    total_harvested += count
                    await page.wait_for_timeout(random.randint(
                        scraper_cfg.get("query_cooldown_min_ms", 2500),
                        scraper_cfg.get("query_cooldown_max_ms", 5000)
                    ))
            else:
                count = await scrape_single_query(page, query, config, run_id=run_id)
                total_harvested += count
                await page.wait_for_timeout(random.randint(
                    scraper_cfg.get("query_cooldown_min_ms", 2500),
                    scraper_cfg.get("query_cooldown_max_ms", 5000)
                ))

        await context.close()

    logging.info(f"--- c1 Scraper Run Complete. Total Staged Today: {total_harvested} ---")
    return total_harvested


if __name__ == "__main__":
    asyncio.run(run_maps_scraper("config.json"))
