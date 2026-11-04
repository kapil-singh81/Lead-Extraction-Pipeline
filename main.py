import argparse
import asyncio
import json
import logging
import sys
import time

import c1_scraper
import auditor
import sheets

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


def parse_arguments() -> argparse.Namespace:
    """Parses command-line arguments for configuration overriding."""
    parser = argparse.ArgumentParser(description="Lead Extraction & Site Auditing Engine")
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to the configuration JSON file (default: config.json)"
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Target Google Maps search query (e.g. 'commercial roofer Dallas TX')"
    )
    parser.add_argument(
        "--country",
        type=str,
        default=None,
        help="Target country code (e.g. 'US') for phone formatting and location parsing"
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip Stage 1 Maps extraction"
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip Stage 2 & 3 site auditing and qualification"
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip Stage 4 Google Sheets export"
    )
    return parser.parse_args()


async def run_pipeline(
    config_path: str,
    query: str = None,
    country: str = None,
    args: argparse.Namespace = None
) -> None:
    """Executes the pipeline stages sequentially with optional runtime query/country overrides."""
    start_time = time.time()
    logging.info(f"=== Starting Lead Extraction Pipeline [Config: {config_path}] ===")

    if args is None:
        class DummyArgs:
            skip_scrape = False
            skip_audit = False
            skip_sync = False
        args = DummyArgs()

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if query:
        query_clean = query.strip()
        config["scraper_c1"]["target_queries"] = [query_clean]
        logging.info(f"[RUNTIME OVERRIDE] Target query set to: '{query_clean}'")

    if country:
        country_clean = country.strip().upper()
        config["app"]["target_country"] = country_clean
        logging.info(f"[RUNTIME OVERRIDE] Target country set to: '{country_clean}'")

        country_prefix_map = {
            "US": "+1", "CA": "+1",
            "IN": "+91",
            "UK": "+44", "GB": "+44",
            "AU": "+61",
        }
        if country_clean in country_prefix_map:
            config["scraper_c1"]["default_phone_prefix"] = country_prefix_map[country_clean]
            logging.info(f"[RUNTIME OVERRIDE] Phone prefix set to: '{country_prefix_map[country_clean]}'")

    # 1. Maps Extraction Engine
    if not args.skip_scrape:
        logging.info("--- Stage 1: Running Maps Harvester ---")
        try:
            harvested_count = await c1_scraper.run_maps_scraper(config)
            logging.info(f"Stage 1 Complete. Newly harvested leads: {harvested_count}")
        except Exception as e:
            logging.error(f"Fatal error in Stage 1 (Maps Scraper): {e}", exc_info=True)
            return

    # 2. Path B & Recovery Engine (discovers unlisted domains for established leads)
    if not args.skip_audit:
        logging.info("--- Stage 2: Processing No-Website Leads & Cross-Directory Recovery ---")
        try:
            auditor.process_no_website_leads(config)
            logging.info("Stage 2 Complete. No-website leads processed & cross-directory recovery finished.")
        except Exception as e:
            logging.error(f"Error in Stage 2 (No-Website Recovery): {e}", exc_info=True)

    # 3. Path A Audit Engine (audits standard sites + newly recovered domains)
    if not args.skip_audit:
        logging.info("--- Stage 3: Running Conversion & Quality Auditor ---")
        try:
            await auditor.run_audit_pipeline(config)
            logging.info("Stage 3 Complete. All pending leads audited and classified.")
        except Exception as e:
            logging.error(f"Error in Stage 3 (Site Auditor): {e}", exc_info=True)

    # 4. Google Sheets Sync Engine
    if not args.skip_sync:
        logging.info("--- Stage 4: Syncing Leads to Google Sheets ---")
        try:
            synced_count = sheets.sync_to_sheets(config)
            logging.info(f"Stage 4 Complete. Leads pushed to Google Sheets: {synced_count}")
        except Exception as e:
            logging.error(f"Error in Stage 4 (Google Sheets Sync): {e}", exc_info=True)
            logging.warning("Data remains safely cached in SQLite and can be synced on the next run.")

    elapsed = round(time.time() - start_time, 2)
    logging.info(f"=== Pipeline Cycle Finished in {elapsed}s ===")


def main() -> None:
    """Entry point."""
    args = parse_arguments()
    try:
        asyncio.run(run_pipeline(
            config_path=args.config,
            query=args.query,
            country=args.country,
            args=args
        ))
    except KeyboardInterrupt:
        logging.warning("Pipeline execution interrupted by user. Exiting cleanly.")
    except Exception as e:
        logging.critical(f"Unhandled system crash: {e}", exc_info=True)


if __name__ == "__main__":
    main()
