"""
Core pipeline package for Google Maps lead extraction, mobile auditing,
qualification scoring, SQLite WAL storage, and outreach pitch generation.
"""

from .database import (
    init_db,
    get_connection,
    insert_lead,
    lead_exists,
    update_lead_audit,
    get_daily_queue,
    get_contacted_leads_with_history,
    get_contacted_stats,
    log_outreach,
    get_lead_outreach_history,
    get_lead_by_id,
    update_lead_fields,
    get_unaudited_leads,
    get_leads_for_reaudit,
    get_pipeline_stats,
    create_run,
    complete_run,
)
from .classifier import (
    classify_lead,
    sanitize_url,
    sanitize_text,
    sanitize_phone,
    sanitize_email,
)
from .c1_scraper import run_maps_scraper
from .auditor import (
    run_site_auditor,
    run_audit_pipeline,
    audit_single_site,
)
from .pitch_generator import (
    generate_pitches,
    clean_contractor_name,
    get_proof_asset,
    get_flaw_variants,
)

__all__ = [
    "init_db",
    "get_connection",
    "insert_lead",
    "lead_exists",
    "update_lead_audit",
    "get_daily_queue",
    "get_contacted_leads_with_history",
    "get_contacted_stats",
    "log_outreach",
    "get_lead_outreach_history",
    "get_lead_by_id",
    "update_lead_fields",
    "get_unaudited_leads",
    "get_leads_for_reaudit",
    "get_pipeline_stats",
    "create_run",
    "complete_run",
    "classify_lead",
    "sanitize_url",
    "sanitize_text",
    "sanitize_phone",
    "sanitize_email",
    "run_maps_scraper",
    "run_site_auditor",
    "run_audit_pipeline",
    "audit_single_site",
    "generate_pitches",
    "clean_contractor_name",
    "get_proof_asset",
    "get_flaw_variants",
]
