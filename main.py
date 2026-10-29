import argparse
import asyncio
import json
import logging
import sys
import time

from c1_scraper import run_maps_scraper
from auditor import run_site_auditor
from sheets import sync_to_sheets

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
    return parser.parse_args()


async def run_pipeline(config_path: str, query: str = None, country: str = None) -> None:
    """Executes the three pipeline stages sequentially with optional runtime query/country overrides."""
    start_time = time.time()
    logging.info(f"=== Starting Lead Extraction Pipeline [Config: {config_path}] ===")

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

    # --- Stage 1: Google Maps Harvester (c1) ---
    logging.info("--- [STAGE 1/3] Running Google Maps Extractor ---")
    try:
        harvested_count = await run_maps_scraper(config)
        logging.info(f"Stage 1 Complete. Newly harvested leads: {harvested_count}")
    except Exception as e:
        logging.error(f"Fatal error in Stage 1 (Maps Scraper): {e}", exc_info=True)
        return

    # --- Stage 2: Technical Site Auditor & Qualification ---
    logging.info("--- [STAGE 2/3] Running Parallel Site Auditor & Classifier ---")
    try:
        await run_site_auditor(config)
        logging.info("Stage 2 Complete. All pending leads audited and classified.")
    except Exception as e:
        logging.error(f"Error in Stage 2 (Site Auditor): {e}", exc_info=True)
        # Continue to sync whatever succeeded

    # --- Stage 3: Google Sheets Sync Bridge ---
    logging.info("--- [STAGE 3/3] Exporting Audited Leads to Google Sheets ('Today' Tab) ---")
    try:
        synced_count = sync_to_sheets(config)
        logging.info(f"Stage 3 Complete. Leads pushed to Google Sheets: {synced_count}")
    except Exception as e:
        logging.error(f"Error in Stage 3 (Google Sheets Sync): {e}", exc_info=True)
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
            country=args.country
        ))
    except KeyboardInterrupt:
        logging.warning("Pipeline execution interrupted by user. Exiting cleanly.")
    except Exception as e:
        logging.critical(f"Unhandled system crash: {e}", exc_info=True)


if __name__ == "__main__":
    main()
