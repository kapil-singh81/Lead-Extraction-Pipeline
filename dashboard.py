import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Union

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import database, classifier
from core.pitch_generator import generate_pitches

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent

# Config & DB helpers with mtime-based in-memory caching
_CONFIG_CACHE: Optional[Dict[str, Any]] = None
_CONFIG_MTIME: Optional[float] = None
_CONFIG_PATH: Optional[str] = None


def load_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Loads application config from config.json with mtime-based in-memory caching."""
    global _CONFIG_CACHE, _CONFIG_MTIME, _CONFIG_PATH
    if not config_path:
        env_cfg = os.environ.get("CONFIG_PATH") or os.environ.get("LEAD_PIPELINE_CONFIG")
        if env_cfg:
            config_path = env_cfg
    cfg_path = Path(config_path) if config_path else (BASE_DIR / "config.json")
    path_str = str(cfg_path)
    try:
        current_mtime = cfg_path.stat().st_mtime
        if _CONFIG_CACHE is not None and _CONFIG_PATH == path_str and _CONFIG_MTIME == current_mtime:
            return _CONFIG_CACHE
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            _CONFIG_CACHE = cfg
            _CONFIG_MTIME = current_mtime
            _CONFIG_PATH = path_str
            return cfg
    except Exception as e:
        logging.error(f"Failed to load config.json from {cfg_path}: {e}")
        return {}


def get_db_path(cfg: Dict[str, Any]) -> str:
    """Returns configured SQLite database path, defaulting to data/lead_pipeline.db."""
    return cfg.get("app", {}).get("db_path", "data/lead_pipeline.db")


def _reclassify_after_contact_edit(lead: Dict[str, Any], cfg: Dict[str, Any], db_path: str) -> Dict[str, Any]:
    """
    Reclassifies a lead after contact information is edited or added.
    Runs ONLY when the lead has completed audit and status is in ('missing_info', 'outreach_ready').
    Syncs current contact keys to audit_data to avoid resurrecting stale values.
    """
    if not lead:
        return lead
    audit_raw = lead.get("audit_json")
    if audit_raw and isinstance(audit_raw, str):
        try:
            audit_data = json.loads(audit_raw)
        except Exception:
            audit_data = {}
    elif isinstance(audit_raw, dict):
        audit_data = dict(audit_raw)
    else:
        audit_data = {}

    if not (audit_data.get("audit_completed") and lead.get("status") in ("missing_info", "outreach_ready")):
        return lead

    sync_keys = [
        "dm_name", "dm_title", "dm_email", "dm_linkedin",
        "generic_email", "facebook_url", "instagram_url",
        "whatsapp_url", "form_url", "line_type"
    ]
    for k in sync_keys:
        audit_data[k] = lead.get(k)

    new_status, new_reason, new_score = classifier.classify_lead(lead, audit_data, cfg)
    database.update_lead_audit(lead["id"], audit_data, new_status, new_reason, new_score, db_path=db_path)
    return database.get_lead_by_id(lead["id"], db_path=db_path) or lead


def _attach_drawer_defaults(lead_dict: Optional[Dict[str, Any]], history: list) -> Optional[Dict[str, Any]]:
    """
    Populates last_channel, status, and last_outcome defaults on unpacked lead dictionary
    for the Slide-Over Drawer Status Manager.
    """
    if not lead_dict:
        return None
    if history:
        lead_dict["last_channel"] = (history[0].get("channel") or "").lower()
        lead_dict["last_outcome"] = (history[0].get("outcome") or "sent").lower()
    else:
        lead_dict["last_outcome"] = "sent"
        if lead_dict.get("whatsapp_url"):
            ch = "whatsapp"
        elif lead_dict.get("dm_email") or lead_dict.get("generic_email"):
            ch = "email"
        elif lead_dict.get("clean_phone") or lead_dict.get("phone"):
            ch = "phone"
        elif lead_dict.get("form_url"):
            ch = "form"
        elif lead_dict.get("facebook_url"):
            ch = "facebook"
        else:
            ch = "whatsapp"
        lead_dict["last_channel"] = ch
    return lead_dict


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes SQLite database with WAL mode on server startup."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    database.init_db(db_path)
    logging.info(f"Dashboard connected to SQLite WAL archive at: {db_path}")
    yield


app = FastAPI(
    title="Lead Extraction Micro-CRM",
    description="Daily Lead Verification & Outreach Cockpit",
    lifespan=lifespan
)

@app.middleware("http")
async def add_no_cache_for_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Static assets and Jinja2 templates (resilient path anchors)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def format_external_url(url: Optional[str]) -> str:
    """Ensures external website link has an http:// or https:// protocol."""
    if not url:
        return ""
    u = str(url).strip()
    if u.lower() == "none":
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        return f"https://{u}"
    return u


templates.env.filters["external_url"] = format_external_url


def unpack_audit(
    lead: Optional[Dict[str, Any]],
    include_pitches: bool = True,
    include_history: Optional[bool] = None,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Helper to deserialize audit_json into a Python dict for Jinja2 templates."""
    if not lead:
        return None
    lead_copy = dict(lead)
    audit_raw = lead_copy.get("audit_json")
    if audit_raw and isinstance(audit_raw, str):
        try:
            lead_copy["audit"] = json.loads(audit_raw)
        except Exception:
            lead_copy["audit"] = {}
    elif isinstance(audit_raw, dict):
        lead_copy["audit"] = audit_raw
    else:
        lead_copy["audit"] = {}

    # Normalize website URL with https:// if scheme is missing
    site = lead_copy.get("website_url")
    if site and isinstance(site, str) and site.strip() and site.strip().lower() != "none":
        s = site.strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            lead_copy["website_url"] = f"https://{s}"

    # On-demand calculation only: compute pitches when viewing single card or drawer
    if include_pitches:
        pitches_data = generate_pitches(lead_copy)
        lead_copy["pitches"] = pitches_data

        rec_key = pitches_data.get("recommended", "naive_curiosity")
        rec_approach = pitches_data.get("approaches", {}).get(rec_key, {})
        lead_copy["recommended_pitch"] = rec_approach

        # Assign WhatsApp launch URL whenever available from recommended pitch
        lead_copy["whatsapp_launch_url"] = rec_approach.get("wa_url")
    else:
        lead_copy["pitches"] = None
        lead_copy["recommended_pitch"] = None
        lead_copy["whatsapp_launch_url"] = None

    # Only assign verified on-site WhatsApp URL if present and not explicitly removed
    audit_dict = lead_copy.get("audit") or {}
    wa_url = lead_copy.get("whatsapp_url")
    if not wa_url:
        if (
            audit_dict.get("whatsapp_source") != "removed"
            and bool(audit_dict.get("is_whatsapp_active"))
            and audit_dict.get("whatsapp_url")
        ):
            wa_url = audit_dict.get("whatsapp_url")

    if wa_url:
        lead_copy["whatsapp_url"] = wa_url
        lead_copy["has_whatsapp"] = True
    else:
        lead_copy["whatsapp_url"] = None
        lead_copy["has_whatsapp"] = bool(lead_copy.get("whatsapp_launch_url"))

    # Outreach history resolution
    if include_history is None:
        include_history = include_pitches

    if include_history:
        if "id" in lead_copy and not lead_copy.get("history"):
            try:
                resolved_db = db_path if db_path is not None else get_db_path(load_config())
                lead_copy["history"] = database.get_lead_outreach_history(lead_copy["id"], db_path=resolved_db)
            except Exception:
                lead_copy["history"] = []
    else:
        if "history" not in lead_copy:
            lead_copy["history"] = []

    return lead_copy


@app.get("/", response_class=HTMLResponse)
async def get_daily_queue_view(
    request: Request,
    lead_id: Optional[int] = None,
    view: Optional[str] = "split",
    search: Optional[str] = None
):
    """Renders the Daily Verification Cockpit with split-view and all-cards grid support."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)

    raw_leads = queue_data.get("leads", [])
    queue_leads = [unpack_audit(l, include_pitches=(view == "grid"), include_history=False, db_path=db_path) for l in raw_leads]

    if search:
        s_lower = search.lower().strip()
        queue_leads = [
            l for l in queue_leads
            if s_lower in (l.get("business_name") or "").lower()
            or s_lower in (l.get("city") or "").lower()
            or s_lower in (l.get("niche") or "").lower()
            or any(s_lower in f.lower() for f in (l.get("audit", {}).get("flaw_labels") or []))
        ]

    selected_lead = None
    selected_raw = None
    if lead_id:
        selected_raw = next((l for l in queue_leads if l.get("id") == lead_id), None)
        if not selected_raw:
            selected_raw = next((l for l in raw_leads if l.get("id") == lead_id), None)
        if not selected_raw:
            selected_raw = database.get_lead_by_id(lead_id, db_path=db_path)
    elif queue_leads:
        selected_raw = queue_leads[0]

    if selected_raw:
        selected_lead = unpack_audit(selected_raw, include_pitches=True, include_history=True, db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "queue_leads": queue_leads,
            "selected_lead": selected_lead,
            "lead": selected_lead,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats,
            "active_nav": "queue",
            "view_mode": view or "split",
            "search": search or ""
        }
    )


@app.get("/api/leads/{lead_id}/card", response_class=HTMLResponse)
async def get_lead_card_partial(request: Request, lead_id: int):
    """Returns single detailed lead inspection card for interactive selection."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    unpacked_lead = unpack_audit(lead, include_pitches=True, db_path=db_path)
    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_card.html",
        context={
            "lead": unpacked_lead,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats
        }
    )


@app.get("/api/leads/{lead_id}/modal", response_class=HTMLResponse)
async def get_lead_modal_partial(request: Request, lead_id: int):
    """Returns slide-over inspection card for any lead in master leads archive."""
    cfg = load_config()
    db_path = get_db_path(cfg)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    unpacked_lead = unpack_audit(lead, include_pitches=True, include_history=False, db_path=db_path)
    history = database.get_lead_outreach_history(lead_id, db_path=db_path)
    unpacked_lead = _attach_drawer_defaults(unpacked_lead, history)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_drawer.html",
        context={
            "lead": unpacked_lead,
            "history": history
        }
    )


@app.post("/api/leads/{lead_id}/track-pitch")
async def track_pitch_selection(
    lead_id: int,
    pitch_type: str = Form(...)
):
    """Tracks the latest copied pitch type across all 8 strategy models."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    pitch_map = {
        "naive_curiosity": "Curiosity",
        "loss_aversion": "Loss Aversion",
        "accusation_audit": "Accusation",
        "no_oriented": "No-Oriented",
        "local_contrast": "Local Contrast",
        "reputation_dissonance": "Reputation",
        "gatekeeper": "Gatekeeper",
        "reciprocity": "Reciprocity"
    }
    label = pitch_map.get(pitch_type, pitch_type.replace("_", " ").title())
    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    audit_raw = lead.get("audit_json")
    if audit_raw and isinstance(audit_raw, str):
        try:
            audit = json.loads(audit_raw)
        except Exception:
            audit = {}
    elif isinstance(audit_raw, dict):
        audit = dict(audit_raw)
    else:
        audit = {}

    audit["last_copied_pitch"] = pitch_type
    audit["last_copied_pitch_label"] = label
    database.update_lead_fields(lead_id, {"audit_json": json.dumps(audit)}, db_path=db_path)
    return {"status": "ok", "lead_id": lead_id, "tracked_pitch": label}


@app.post("/api/leads/{lead_id}/action", response_class=HTMLResponse)
async def post_lead_action(
    request: Request,
    lead_id: int,
    action: str = Form(...),
    channel: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    outcome: Optional[str] = Form("sent"),
    stay: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    pitch_type: Optional[str] = Form(None),
    message_text: Optional[str] = Form(None),
    in_grid: Optional[str] = Form(None)
):
    """
    Inline action handler for daily queue verification card and contacted lead drawer.
    Supports staying on current lead to review/edit, drawer inline update, or advancing to next lead.
    Tracks and records the exact active 1-of-8 strategy key plus final edited textarea value.
    """
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    if action == "contacted":
        outreach_ch = channel or "whatsapp"
        pitch_map = {
            "naive_curiosity": "Curiosity",
            "loss_aversion": "Loss Aversion",
            "accusation_audit": "Accusation",
            "no_oriented": "No-Oriented",
            "local_contrast": "Local Contrast",
            "reputation_dissonance": "Reputation",
            "gatekeeper": "Gatekeeper",
            "reciprocity": "Reciprocity"
        }
        if pitch_type and pitch_type.strip():
            strategy_key = pitch_type.strip()
        else:
            current_l = database.get_lead_by_id(lead_id, db_path=db_path)
            if current_l:
                unp = unpack_audit(current_l, include_pitches=True, include_history=False, db_path=db_path)
                strategy_key = unp.get("pitches", {}).get("recommended", "naive_curiosity") if unp else "naive_curiosity"
            else:
                strategy_key = "naive_curiosity"

        pitch_label = pitch_map.get(strategy_key, strategy_key.replace("_", " ").title())

        # Record exact strategy key and user edited textarea text if provided
        if message_text and message_text.strip():
            final_logged_msg = f"[{strategy_key}] {message_text.strip()}"
        else:
            final_logged_msg = f"[{strategy_key}]"

        database.log_outreach(
            lead_id=lead_id,
            channel=outreach_ch,
            outcome=outcome or "sent",
            message_sent=final_logged_msg,
            notes=notes if notes and notes != pitch_label and notes != "None" else None,
            db_path=db_path
        )
    elif action == "disqualified":
        review_note = f"[reviewed] {notes or reason or 'Manual Disqualification'}".strip()
        database.update_lead_status(
            lead_id=lead_id,
            new_status="disqualified",
            reason=reason or "Manual Disqualification",
            notes=review_note,
            db_path=db_path
        )
    elif action == "missing_info":
        review_note = f"[reviewed] {notes or reason or 'Moved to Missing Info'}".strip()
        database.update_lead_status(
            lead_id=lead_id,
            new_status="missing_info",
            reason=reason or "Moved to Missing Info",
            notes=review_note,
            db_path=db_path
        )
    elif action == "defer":
        deferred_until = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        review_note = f"[reviewed] {notes or 'Deferred 24h by user'}".strip()
        database.update_lead_status(
            lead_id=lead_id,
            new_status="deferred",
            reason="Deferred 24h by user",
            notes=review_note,
            deferred_until=deferred_until,
            db_path=db_path
        )
    elif action == "outreach_ready":
        database.update_lead_status(
            lead_id=lead_id,
            new_status="outreach_ready",
            reason="Kept Outreach Ready",
            notes=notes,
            db_path=db_path
        )

    # If action was triggered from contacted lead drawer
    if stay == "drawer" or source == "drawer":
        current_lead = database.get_lead_by_id(lead_id, db_path=db_path)
        unpacked_current = unpack_audit(current_lead, include_pitches=True, include_history=False, db_path=db_path)
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
        unpacked_current = _attach_drawer_defaults(unpacked_current, history)
        return templates.TemplateResponse(
            request=request,
            name="partials/lead_drawer.html",
            context={
                "lead": unpacked_current,
                "history": history,
                "status_saved": True,
                "status_message": f"✓ Status updated to '{action.replace('_', ' ').title()}' ({channel or 'Logged'})"
            }
        )

    # If user selected to stay on the lead in card view or action is from cards grid
    if stay == "true" or in_grid == "true":
        current_lead = database.get_lead_by_id(lead_id, db_path=db_path)
        unpacked_current = unpack_audit(current_lead, include_pitches=True, db_path=db_path)
        queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
        stats = database.get_pipeline_stats(db_path=db_path)
        raw_leads = queue_data.get("leads", [])
        next_leads = [unpack_audit(l, include_pitches=(in_grid == "true"), include_history=False, db_path=db_path) for l in raw_leads]

        return templates.TemplateResponse(
            request=request,
            name="partials/lead_card.html",
            context={
                "lead": unpacked_current,
                "selected_lead": unpacked_current,
                "queue_leads": next_leads,
                "status_saved": True,
                "status_message": f"✓ Status updated to '{action.replace('_', ' ').title()}' ({channel or 'Logged'})",
                "done_today": queue_data.get("done_today", 0),
                "daily_queue_size": daily_size,
                "remaining_quota": queue_data.get("remaining_quota", 0),
                "stats": stats,
                "in_grid": (in_grid == "true")
            }
        )

    # Otherwise fetch next lead in line and advance queue
    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)
    raw_leads = queue_data.get("leads", [])
    next_leads = [unpack_audit(l, include_pitches=False, include_history=False, db_path=db_path) for l in raw_leads]
    next_lead = unpack_audit(raw_leads[0], include_pitches=True, db_path=db_path) if raw_leads else None

    return templates.TemplateResponse(
        request=request,
        name="partials/action_response.html",
        context={
            "lead": next_lead,
            "selected_lead": next_lead,
            "queue_leads": next_leads,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats
        }
    )


@app.post("/api/leads/{lead_id}/mark-facebook", response_class=HTMLResponse)
async def mark_lead_facebook(
    request: Request,
    lead_id: int,
    notes: Optional[str] = Form(None),
    message_text: Optional[str] = Form(None),
    pitch_type: Optional[str] = Form(None),
    stay: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    in_grid: Optional[str] = Form(None)
):
    """
    Direct backend action endpoint to mark a lead as contacted via Facebook.
    Logs outreach touch with channel='facebook' and updates status to 'contacted'.
    """
    return await post_lead_action(
        request=request,
        lead_id=lead_id,
        action="contacted",
        channel="facebook",
        reason="Contacted via facebook",
        notes=notes,
        outcome="sent",
        stay=stay,
        source=source,
        pitch_type=pitch_type,
        message_text=message_text,
        in_grid=in_grid
    )


@app.post("/api/leads/{lead_id}/update", response_class=HTMLResponse)
async def post_lead_update(
    request: Request,
    lead_id: int,
    business_name: Optional[str] = Form(None),
    website_url: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    clean_phone: Optional[str] = Form(None),
    line_type: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    niche: Optional[str] = Form(None),
    dm_name: Optional[str] = Form(None),
    dm_title: Optional[str] = Form(None),
    dm_email: Optional[str] = Form(None),
    dm_linkedin: Optional[str] = Form(None),
    generic_email: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    status_reason: Optional[str] = Form(None),
    manual_notes: Optional[str] = Form(None),
    whatsapp_url: Optional[str] = Form(None),
    source: Optional[str] = Form(None)
):
    """Updates editable lead information in the SQLite database and refreshes the card or drawer in-place."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    existing = database.get_lead_by_id(lead_id, db_path=db_path)
    if not existing:
        raise HTTPException(status_code=404, detail="Lead not found")

    raw_form = await request.form()
    if phone is None and "phone" in raw_form:
        phone = raw_form.get("phone")
    if clean_phone is None and "clean_phone" in raw_form:
        clean_phone = raw_form.get("clean_phone")
    if website_url is None and "website_url" in raw_form:
        website_url = raw_form.get("website_url")
    if line_type is None and "line_type" in raw_form:
        line_type = raw_form.get("line_type")
    if city is None and "city" in raw_form:
        city = raw_form.get("city")
    if niche is None and "niche" in raw_form:
        niche = raw_form.get("niche")
    if dm_name is None and "dm_name" in raw_form:
        dm_name = raw_form.get("dm_name")
    if dm_title is None and "dm_title" in raw_form:
        dm_title = raw_form.get("dm_title")
    if dm_email is None and "dm_email" in raw_form:
        dm_email = raw_form.get("dm_email")
    if dm_linkedin is None and "dm_linkedin" in raw_form:
        dm_linkedin = raw_form.get("dm_linkedin")
    if generic_email is None and "generic_email" in raw_form:
        generic_email = raw_form.get("generic_email")
    if status_reason is None and "status_reason" in raw_form:
        status_reason = raw_form.get("status_reason")
    if manual_notes is None and "manual_notes" in raw_form:
        manual_notes = raw_form.get("manual_notes")
    if whatsapp_url is None and "whatsapp_url" in raw_form:
        whatsapp_url = raw_form.get("whatsapp_url")

    fields_to_update = {}
    if business_name is not None and business_name.strip():
        fields_to_update["business_name"] = business_name.strip()
    if website_url is not None:
        fields_to_update["website_url"] = website_url.strip() if website_url.strip() else None
    if phone is not None:
        if phone.strip():
            fields_to_update["phone"] = phone.strip()
        else:
            fields_to_update["phone"] = None
            fields_to_update["clean_phone"] = None
    elif clean_phone is not None:
        if clean_phone.strip():
            fields_to_update["clean_phone"] = clean_phone.strip()
        else:
            fields_to_update["clean_phone"] = None
    if line_type is not None:
        fields_to_update["line_type"] = line_type.strip() if line_type.strip() else None
    if city is not None:
        fields_to_update["city"] = city.strip() if city.strip() else None
    if niche is not None:
        fields_to_update["niche"] = niche.strip() if niche.strip() else None
    if dm_name is not None:
        fields_to_update["dm_name"] = dm_name.strip() if dm_name.strip() else None
    if dm_title is not None:
        fields_to_update["dm_title"] = dm_title.strip() if dm_title.strip() else None
    if dm_email is not None:
        fields_to_update["dm_email"] = dm_email.strip() if dm_email.strip() else None
    if dm_linkedin is not None:
        fields_to_update["dm_linkedin"] = dm_linkedin.strip() if dm_linkedin.strip() else None
    if generic_email is not None:
        fields_to_update["generic_email"] = generic_email.strip() if generic_email.strip() else None
    if status is not None and status.strip():
        fields_to_update["status"] = status.strip()
    if status_reason is not None:
        fields_to_update["status_reason"] = status_reason.strip() if status_reason.strip() else None
    if manual_notes is not None:
        fields_to_update["manual_notes"] = manual_notes.strip() if manual_notes.strip() else None
    if whatsapp_url is not None:
        fields_to_update["whatsapp_url"] = whatsapp_url.strip() if whatsapp_url.strip() else None

    updated = database.update_lead_fields(lead_id, fields_to_update, db_path=db_path)
    contact_fields = {"website_url", "phone", "clean_phone", "line_type", "dm_email", "generic_email", "whatsapp_url"}
    if (contact_fields & set(fields_to_update.keys())) and (status is None or status.strip() == existing.get("status")):
        updated = _reclassify_after_contact_edit(updated, cfg, db_path)

    unpacked_lead = unpack_audit(updated, include_pitches=True, include_history=(source != "drawer"), db_path=db_path)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
        unpacked_lead = _attach_drawer_defaults(unpacked_lead, history)
        return templates.TemplateResponse(
            request=request,
            name="partials/lead_drawer.html",
            context={
                "lead": unpacked_lead,
                "history": history,
                "status_saved": True,
                "status_message": "✓ Contractor details updated in SQLite!"
            }
        )

    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    raw_leads = queue_data.get("leads", [])
    next_leads = [unpack_audit(l, include_pitches=False, include_history=False, db_path=db_path) for l in raw_leads]
    stats = database.get_pipeline_stats(db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_card.html",
        context={
            "lead": unpacked_lead,
            "lead_updated": True,
            "status_message": "✓ Lead details updated and saved to SQLite!",
            "queue_leads": next_leads,
            "selected_lead": unpacked_lead,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats
        }
    )


@app.post("/api/leads/{lead_id}/toggle-whatsapp", response_class=HTMLResponse)
async def toggle_whatsapp_channel(
    request: Request,
    lead_id: int,
    source: Optional[str] = None
):
    """
    Toggles WhatsApp as an available outreach channel for a lead.
    If WhatsApp is not active: formats phone number into wa.me link and saves to SQLite.
    If WhatsApp is already active: removes whatsapp_url (NULL) in SQLite.
    Returns refreshed lead card or drawer in-place.
    """
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    audit_raw = lead.get("audit_json")
    if audit_raw and isinstance(audit_raw, str):
        try:
            audit = json.loads(audit_raw)
        except Exception:
            audit = {}
    elif isinstance(audit_raw, dict):
        audit = dict(audit_raw)
    else:
        audit = {}

    current_wa = lead.get("whatsapp_url")
    if current_wa and current_wa.strip():
        # Toggle OFF: remove WhatsApp channel and update audit metadata
        audit["whatsapp_url"] = None
        audit["is_whatsapp_active"] = False
        audit["whatsapp_source"] = "removed"
        database.update_lead_fields(lead_id, {"whatsapp_url": None, "audit_json": json.dumps(audit)}, db_path=db_path)
        action_msg = "WhatsApp removed as channel"
    else:
        # Toggle ON: construct direct WhatsApp link using phone digits
        phone = lead.get("clean_phone") or lead.get("phone")
        digits = re.sub(r"\D", "", str(phone)) if phone else ""
        if len(digits) == 10:
            digits = f"1{digits}"

        if len(digits) < 10:
            action_msg = "Cannot enable WhatsApp: add a valid phone number first"
        else:
            new_wa = f"https://wa.me/{digits}"
            audit["whatsapp_url"] = new_wa
            audit["is_whatsapp_active"] = True
            audit["whatsapp_source"] = "manual"
            database.update_lead_fields(lead_id, {"whatsapp_url": new_wa, "audit_json": json.dumps(audit)}, db_path=db_path)
            action_msg = "WhatsApp added as channel"

    updated_lead = database.get_lead_by_id(lead_id, db_path=db_path)
    unpacked = unpack_audit(updated_lead, include_pitches=True, include_history=(source != "drawer"), db_path=db_path)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
        unpacked = _attach_drawer_defaults(unpacked, history)
        return templates.TemplateResponse(
            request=request,
            name="partials/lead_drawer.html",
            context={
                "lead": unpacked,
                "history": history,
                "status_saved": True,
                "status_message": f"✓ {action_msg}"
            }
        )

    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    raw_leads = queue_data.get("leads", [])
    next_leads = [unpack_audit(l, include_pitches=False, include_history=False, db_path=db_path) for l in raw_leads]
    stats = database.get_pipeline_stats(db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_card.html",
        context={
            "lead": unpacked,
            "status_saved": True,
            "status_message": f"✓ {action_msg}",
            "queue_leads": next_leads,
            "selected_lead": unpacked,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats
        }
    )


@app.post("/api/leads/{lead_id}/add-channel", response_class=HTMLResponse)
async def post_add_channel(
    request: Request,
    lead_id: int,
    channel_type: str = Form(...),
    channel_url: str = Form(...),
    source: Optional[str] = Form(None)
):
    """Adds a custom digital channel (Facebook, Instagram, LinkedIn, Contact Form, etc.) to a lead."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    clean_url = channel_url.strip()
    if clean_url and not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = f"https://{clean_url}"

    fields_to_update = {}
    ch = channel_type.lower()
    if ch == "facebook":
        fields_to_update["facebook_url"] = clean_url
    elif ch == "instagram":
        fields_to_update["instagram_url"] = clean_url
    elif ch == "whatsapp":
        fields_to_update["whatsapp_url"] = clean_url
    elif ch == "form":
        fields_to_update["form_url"] = clean_url
    elif ch == "linkedin":
        fields_to_update["dm_linkedin"] = clean_url
    else:
        try:
            audit = json.loads(lead.get("audit_json") or "{}")
        except Exception:
            audit = {}
        if ch == "twitter":
            audit["twitter_url"] = clean_url
        else:
            custom = audit.get("custom_channels", {})
            custom[ch] = clean_url
            audit["custom_channels"] = custom
        fields_to_update["audit_json"] = json.dumps(audit)

    database.update_lead_fields(lead_id, fields_to_update, db_path=db_path)
    updated_lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if ch in ("facebook", "instagram", "form", "whatsapp", "linkedin") and updated_lead.get("status") in ("missing_info", "outreach_ready"):
        updated_lead = _reclassify_after_contact_edit(updated_lead, cfg, db_path)
    unpacked = unpack_audit(updated_lead, include_pitches=True, include_history=(source != "drawer"), db_path=db_path)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
        unpacked = _attach_drawer_defaults(unpacked, history)
        return templates.TemplateResponse(
            request=request,
            name="partials/lead_drawer.html",
            context={
                "lead": unpacked,
                "history": history,
                "status_saved": True,
                "status_message": f"✓ {channel_type.title()} channel added successfully!"
            }
        )

    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    raw_leads = queue_data.get("leads", [])
    next_leads = [unpack_audit(l, include_pitches=False, include_history=False, db_path=db_path) for l in raw_leads]
    stats = database.get_pipeline_stats(db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_card.html",
        context={
            "lead": unpacked,
            "status_saved": True,
            "status_message": f"✓ {channel_type.title()} channel added successfully!",
            "queue_leads": next_leads,
            "selected_lead": unpacked,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "stats": stats
        }
    )


@app.post("/api/leads/{lead_id}/outreach/{log_id}/delete", response_class=HTMLResponse)
async def delete_outreach_entry(
    request: Request,
    lead_id: int,
    log_id: int,
    source: Optional[str] = Form("drawer")
):
    """Deletes an erroneous outreach log entry and refreshes the lead drawer."""
    cfg = load_config()
    db_path = get_db_path(cfg)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    database.delete_outreach_log(log_id, db_path=db_path)
    history = database.get_lead_outreach_history(lead_id, db_path=db_path)

    status_message = "✓ Outreach entry removed"
    if lead.get("status") == "contacted":
        if not history:
            audit_raw = lead.get("audit_json")
            if audit_raw and isinstance(audit_raw, str):
                try:
                    audit_data = json.loads(audit_raw)
                except Exception:
                    audit_data = {}
            elif isinstance(audit_raw, dict):
                audit_data = dict(audit_raw)
            else:
                audit_data = {}

            if audit_data.get("audit_completed"):
                for k in ("dm_name", "dm_title", "dm_email", "dm_linkedin", "generic_email", "facebook_url", "instagram_url", "whatsapp_url", "form_url", "line_type"):
                    audit_data[k] = lead.get(k)
                new_status, new_reason, new_score = classifier.classify_lead(lead, audit_data, cfg)
                database.update_lead_audit(lead_id, audit_data, new_status, new_reason, new_score, db_path=db_path)
            else:
                database.update_lead_status(lead_id, "outreach_ready", reason="Outreach log cleared", db_path=db_path)
            lead = database.get_lead_by_id(lead_id, db_path=db_path) or lead
            status_message = f"✓ Outreach entry removed (status reverted to {lead.get('status', '').replace('_', ' ').title()})"
        else:
            latest_ch = history[0].get("channel", "unknown")
            database.update_lead_fields(lead_id, {"status_reason": f"Contacted via {latest_ch}"}, db_path=db_path)
            lead = database.get_lead_by_id(lead_id, db_path=db_path) or lead
            status_message = "✓ Outreach entry removed"

    unpacked = unpack_audit(lead, include_pitches=True, include_history=False, db_path=db_path)
    unpacked = _attach_drawer_defaults(unpacked, history)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_drawer.html",
        context={
            "lead": unpacked,
            "history": history,
            "status_saved": True,
            "status_message": status_message
        }
    )


@app.get("/contacted", response_class=HTMLResponse)
async def get_contacted_leads_view(
    request: Request,
    channel: Optional[str] = "all",
    search: Optional[str] = None,
    view: Optional[str] = "table",
    page: int = 1
):
    """Renders the dedicated Contacted Leads view tracking all outreach interactions."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    limit = 50
    offset = max(0, (page - 1) * limit)

    contacted_leads, total_count = database.get_contacted_leads_with_history(
        search=search,
        channel=channel,
        limit=limit,
        offset=offset,
        db_path=db_path
    )
    unpacked_leads = [unpack_audit(l, include_pitches=(view == "grid"), include_history=False, db_path=db_path) for l in contacted_leads]
    contacted_stats = database.get_contacted_stats(db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)
    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="contacted.html",
        context={
            "contacted_leads": unpacked_leads,
            "total_count": total_count,
            "current_channel": channel or "all",
            "search": search or "",
            "view_mode": view or "table",
            "page": page,
            "contacted_stats": contacted_stats,
            "stats": stats,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "active_nav": "contacted"
        }
    )


@app.get("/leads", response_class=HTMLResponse)
async def get_master_leads_view(
    request: Request,
    status: Optional[str] = "all",
    search: Optional[str] = None,
    channel: Optional[str] = "all",
    flaw_type: Optional[str] = None,
    min_rating: Optional[float] = None,
    view: Optional[str] = "table",
    page: int = 1
):
    """Renders the Master Leads Archive table view with omni-search, status filters and cards grid mode."""
    cfg = load_config()
    db_path = get_db_path(cfg)
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    limit = 50
    offset = max(0, (page - 1) * limit)

    leads, total_count = database.get_leads_master(
        status=status,
        search=search,
        channel=channel,
        flaw_type=flaw_type,
        min_rating=min_rating,
        limit=limit,
        offset=offset,
        db_path=db_path
    )
    unpacked_leads = [unpack_audit(l, include_pitches=(view == "grid"), include_history=False, db_path=db_path) for l in leads]
    stats = database.get_pipeline_stats(db_path=db_path)
    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="leads_table.html",
        context={
            "leads": unpacked_leads,
            "total_count": total_count,
            "current_status": status,
            "search": search or "",
            "current_channel": channel or "all",
            "flaw_type": flaw_type or "",
            "min_rating": min_rating or "",
            "view_mode": view or "table",
            "page": page,
            "stats": stats,
            "done_today": queue_data.get("done_today", 0),
            "daily_queue_size": daily_size,
            "remaining_quota": queue_data.get("remaining_quota", 0),
            "active_nav": "leads"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000, reload=True)
