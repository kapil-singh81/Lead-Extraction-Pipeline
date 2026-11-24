"""
verify_whatsapp_leads.py - Runs the WhatsApp verification engine across all available leads in SQLite.
Discovers on-site WhatsApp chat widgets, floating buttons, wa.me links, and mobile registry channels.
"""

import os
import sys
import asyncio
import json
import logging
import re
import urllib.parse
from typing import Dict, Any, List, Optional
import httpx

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import database, auditor, classifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def verify_lead_whatsapp(
    client: httpx.AsyncClient,
    lead: Dict[str, Any],
    config: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    db_path: str = "data/lead_pipeline.db"
) -> Dict[str, Any]:
    lead_id = lead["id"]
    biz_name = lead.get("business_name") or f"Lead #{lead_id}"
    url = lead.get("website_url") or ""
    clean_phone = lead.get("clean_phone")
    line_type = lead.get("line_type") or "Unknown"

    wa_url = None
    is_active = False
    wa_source = "none"

    if url and url.startswith(("http://", "https://")):
        async with semaphore:
            logging.info(f"[{lead_id}] Checking website for WhatsApp: {url}")
            try:
                resp = await client.get(url, timeout=10.0, follow_redirects=True)
                if resp.status_code < 400:
                    html = resp.text
                    links = re.findall(r'<a\s+(?:[^>]*?\s+)?href=["\']([^"\']+)["\']', html, re.IGNORECASE)
                    
                    wa_url, is_active, wa_source = auditor.extract_whatsapp_info(
                        links, html, clean_phone, line_type
                    )

                    # If not found on homepage, check /contact or /quote subpage if present in links
                    if not is_active:
                        parsed_base = urllib.parse.urlparse(url)
                        base_domain = parsed_base.netloc.lower()
                        for link in links:
                            l_lower = link.lower()
                            if any(k in l_lower for k in ["contact", "quote", "reach-us", "get-in-touch"]):
                                sub_url = urllib.parse.urljoin(url, link)
                                parsed_sub = urllib.parse.urlparse(sub_url)
                                if parsed_sub.netloc.lower() == base_domain:
                                    try:
                                        sub_resp = await client.get(sub_url, timeout=8.0, follow_redirects=True)
                                        if sub_resp.status_code < 400:
                                            sub_html = sub_resp.text
                                            sub_links = re.findall(r'<a\s+(?:[^>]*?\s+)?href=["\']([^"\']+)["\']', sub_html, re.IGNORECASE)
                                            sub_wa_url, sub_is_active, sub_src = auditor.extract_whatsapp_info(
                                                sub_links, sub_html, clean_phone, line_type
                                            )
                                            if sub_is_active:
                                                wa_url, is_active, wa_source = sub_wa_url, sub_is_active, sub_src
                                                break
                                    except Exception:
                                        pass
                                    break
            except Exception as e:
                logging.debug(f"[{lead_id}] HTTP fetch skipped: {e}")

    # Fallback to mobile line check if not already found on website
    if not is_active:
        wa_url, is_active, wa_source = auditor.extract_whatsapp_info([], "", clean_phone, line_type)

    # If WhatsApp is verified or found, update database and recalculate priority score
    audit_data = {}
    audit_raw = lead.get("audit_json")
    if audit_raw:
        try:
            audit_data = json.loads(audit_raw)
        except Exception:
            audit_data = {}

    if is_active and wa_url:
        logging.info(f"==> [{lead_id}] {biz_name}: VERIFIED WHATSAPP ({wa_source}) -> {wa_url}")
        audit_data["whatsapp_url"] = wa_url
        audit_data["is_whatsapp_active"] = True
        audit_data["whatsapp_source"] = wa_source

        lead_enriched = {
            **lead,
            "whatsapp_url": wa_url,
            "line_type": line_type
        }

        # Recalculate deterministic priority score with has_whatsapp_bonus
        status, status_reason, priority_score = classifier.classify_lead(lead_enriched, audit_data, config)

        conn = database.get_connection(db_path)
        try:
            with conn:
                conn.execute("""
                    UPDATE leads
                    SET whatsapp_url = ?,
                        priority_score = ?,
                        status = ?,
                        status_reason = ?,
                        audit_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (wa_url, priority_score, status, status_reason, json.dumps(audit_data), lead_id))
        finally:
            conn.close()

        return {
            "id": lead_id,
            "name": biz_name,
            "whatsapp_url": wa_url,
            "source": wa_source,
            "priority_score": priority_score,
            "status": status
        }
    else:
        # Save audit_data with is_whatsapp_active = False if previously audited
        if audit_data:
            audit_data["is_whatsapp_active"] = False
            audit_data["whatsapp_source"] = "none"
            conn = database.get_connection(db_path)
            try:
                with conn:
                    conn.execute("UPDATE leads SET audit_json = ? WHERE id = ?", (json.dumps(audit_data), lead_id))
            finally:
                conn.close()

    return {
        "id": lead_id,
        "name": biz_name,
        "whatsapp_url": None,
        "source": "none"
    }


async def main():
    db_path = "lead_pipeline.db"
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()
        leads = [dict(r) for r in rows]
    finally:
        conn.close()

    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)

    logging.info(f"Loaded {len(leads)} available leads from SQLite. Starting WhatsApp verification...")
    semaphore = asyncio.Semaphore(8)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    async with httpx.AsyncClient(headers=headers, verify=False, timeout=12.0) as client:
        tasks = [
            verify_lead_whatsapp(client, lead, config, semaphore, db_path=db_path)
            for lead in leads
        ]
        results = await asyncio.gather(*tasks)

    verified = [r for r in results if r.get("whatsapp_url")]
    logging.info("=" * 60)
    logging.info(f"WHATSAPP VERIFICATION COMPLETE: {len(verified)} / {len(leads)} leads have active WhatsApp.")
    for v in verified:
        logging.info(f" - #{v['id']} {v['name']}: {v['whatsapp_url']} (Source: {v['source']}, Score: {v.get('priority_score', 0)})")
    logging.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
