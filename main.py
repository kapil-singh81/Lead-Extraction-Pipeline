import argparse
import asyncio
import json
import logging
import os
import sys
import time

from core import database, c1_scraper, auditor

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

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
    parser = argparse.ArgumentParser(description="Lead Extraction & Site Auditing Engine (Micro-CRM / WAL)")
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
        help="Skip Stage 2 site auditing and qualification"
    )
    parser.add_argument(
        "--re-audit",
        action="store_true",
        help="Re-audit all existing leads in the database that have websites instead of only new leads"
    )
    return parser.parse_args()


async def run_pipeline(
    config_path: str,
    query: str = None,
    country: str = None,
    args: argparse.Namespace = None
) -> None:
    """Executes extraction and auditing stages sequentially, updating SQLite WAL."""
    start_time = time.time()
    logging.info(f"=== Starting Lead Extraction Pipeline [Config: {config_path}] ===")

    if not os.path.isabs(config_path) and not os.path.exists(config_path):
        config_path = os.path.join(PROJECT_ROOT, config_path)

    if args is None:
        class DummyArgs:
            skip_scrape = False
            skip_audit = False
        args = DummyArgs()

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    log_level_name = config.get("app", {}).get("log_level", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.getLogger().setLevel(log_level)

    db_path = config["app"]["db_path"]
    database.init_db(db_path)

    if query:
        query_clean = query.strip()
        config["scraper_c1"]["target_queries"] = [query_clean]
        logging.info(f"[RUNTIME OVERRIDE] Target query set to: '{query_clean}'")

    if country:
        country_clean = country.strip().upper()
        config["app"]["target_country"] = country_clean
        logging.info(f"[RUNTIME OVERRIDE] Target country set to: '{country_clean}'")

        country_prefix_map = config.get("app", {}).get("country_prefix_map", {"US": "+1"})
        if country_clean in country_prefix_map:
            config["scraper_c1"]["default_phone_prefix"] = country_prefix_map[country_clean]
            logging.info(f"[RUNTIME OVERRIDE] Phone prefix set to: '{country_prefix_map[country_clean]}'")

    # Determine run_type accurately from CLI flags
    if getattr(args, "skip_scrape", False) and getattr(args, "re_audit", False):
        run_type = "re_audit"
    elif getattr(args, "skip_scrape", False) and not getattr(args, "skip_audit", False):
        run_type = "audit_only"
    elif getattr(args, "skip_audit", False) and not getattr(args, "skip_scrape", False):
        run_type = "scrape_only"
    else:
        run_type = "local_scrape"

    # Initialize batch run tracking
    active_query = query or (config.get("scraper_c1", {}).get("target_queries") or ["unknown"])[0]
    run_id = database.create_run(
        run_type=run_type,
        query=active_query,
        metadata={"country": config["app"]["target_country"], "db_path": db_path},
        db_path=db_path
    )
    logging.info(f"Initialized Tracking Run #{run_id} ({run_type}) in SQLite archive.")

    before_run_stats = database.get_pipeline_stats(db_path=db_path)
    harvested_count = 0
    pre_reaudit_count = 0
    before_audit_new = 0
    re_audit = getattr(args, "re_audit", False)

    try:
        # 1. Stage 1: Google Maps Extraction Engine
        if not args.skip_scrape:
            logging.info("--- Stage 1: Running Maps Harvester ---")
            harvested_count = await c1_scraper.run_maps_scraper(config, run_id=run_id)
            logging.info(f"Stage 1 Complete. Newly harvested leads: {harvested_count}")
        else:
            logging.info("Skipping Stage 1 (Scraping skipped by flag).")

        # 2. Stage 2: Audit & Deterministic Classification Engine
        if not args.skip_audit:
            if re_audit:
                pre_reaudit_count = len(database.get_leads_for_reaudit(db_path=db_path))
            else:
                before_audit_new = database.get_pipeline_stats(db_path=db_path).get("new", 0)

            logging.info("--- Stage 2: Running Conversion & Quality Auditor ---")
            await auditor.run_audit_pipeline(config, re_audit=re_audit)
            logging.info("Stage 2 Complete. All leads audited and classified.")
        else:
            logging.info("Skipping Stage 2 (Auditing skipped by flag).")

        # Fetch stats and complete run record
        after_stats = database.get_pipeline_stats(db_path=db_path)
        if args.skip_audit:
            audited_this_run = 0
        elif re_audit:
            audited_this_run = pre_reaudit_count
        else:
            audited_this_run = max(0, before_audit_new - after_stats.get("new", 0))

        ready_this_run = max(0, after_stats.get("outreach_ready", 0) - before_run_stats.get("outreach_ready", 0))

        database.complete_run(
            run_id,
            harvested=harvested_count,
            audited=audited_this_run,
            ready=ready_this_run,
            db_path=db_path
        )
    except (Exception, KeyboardInterrupt) as e:
        err_msg = "Interrupted by user" if isinstance(e, KeyboardInterrupt) else str(e)
        logging.error(f"Pipeline run #{run_id} failed: {err_msg}")
        database.fail_run(run_id, error_msg=err_msg, db_path=db_path)
        raise

    stats = after_stats
    elapsed = round(time.time() - start_time, 2)
    logging.info(f"=== Pipeline Cycle Finished in {elapsed}s ===")

    # Display clean CLI Summary
    print("\n" + "=" * 60)
    print("                PIPELINE STATUS SUMMARY")
    print("=" * 60)
    print(f"  Total Stored Leads:     {stats.get('total', 0)}")
    print(f"  Outreach Ready:         {stats.get('outreach_ready', 0)}")
    print(f"  Deferred:               {stats.get('deferred', 0)}")
    print(f"  Replied:                {stats.get('replied', 0)}")
    print(f"  Missing Contact Info:   {stats.get('missing_info', 0)}")
    print(f"  Contacted:              {stats.get('contacted', 0)}")
    print(f"  Disqualified:           {stats.get('disqualified', 0)}")
    print(f"  Pending Audits (New):   {stats.get('new', 0)}")
    print("=" * 60)
    print("  Launch the daily verification cockpit:")
    print("    python dashboard.py")
    print("=" * 60 + "\n")


def main() -> None:
    """Entry point."""
    args = parse_arguments()
    config_path = args.config
    if not os.path.isabs(config_path) and not os.path.exists(config_path):
        config_path = os.path.join(PROJECT_ROOT, config_path)
    try:
        asyncio.run(run_pipeline(
            config_path=config_path,
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
