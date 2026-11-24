import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import database
from core.pitch_generator import generate_pitches

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Lead Extraction Micro-CRM", description="Daily Lead Verification & Outreach Cockpit")

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


def load_config() -> Dict[str, Any]:
    """Loads application config from config.json."""
    try:
        with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def unpack_audit(lead: Optional[Dict[str, Any]], include_pitches: bool = True) -> Optional[Dict[str, Any]]:
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

    # Only assign verified on-site WhatsApp URL if present
    gen_wa_url = lead_copy.get("whatsapp_url") or lead_copy.get("audit", {}).get("whatsapp_url")
    if gen_wa_url:
        lead_copy["whatsapp_url"] = gen_wa_url
        lead_copy["has_whatsapp"] = True
    else:
        lead_copy["whatsapp_url"] = None
        lead_copy["has_whatsapp"] = bool(lead_copy.get("whatsapp_launch_url"))

    # Attach outreach history if not already present
    if "id" in lead_copy and "history" not in lead_copy:
        try:
            cfg = load_config()
            db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
            lead_copy["history"] = database.get_lead_outreach_history(lead_copy["id"], db_path=db_path)
        except Exception:
            lead_copy["history"] = []

    return lead_copy


@app.on_event("startup")
def on_startup():
    """Initializes SQLite database with WAL mode on server start."""
    cfg = load_config()
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    database.init_db(db_path)
    logging.info(f"Dashboard connected to SQLite WAL archive at: {db_path}")


@app.get("/", response_class=HTMLResponse)
async def get_daily_queue_view(
    request: Request,
    lead_id: Optional[int] = None,
    view: Optional[str] = "split",
    search: Optional[str] = None
):
    """Renders the Daily Verification Cockpit with split-view and all-cards grid support."""
    cfg = load_config()
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)

    raw_leads = queue_data.get("leads", [])
    queue_leads = [unpack_audit(l, include_pitches=False) for l in raw_leads]

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
        selected_raw = next((l for l in raw_leads if l.get("id") == lead_id), None)
        if not selected_raw:
            selected_raw = database.get_lead_by_id(lead_id, db_path=db_path)
    elif raw_leads:
        selected_raw = raw_leads[0]

    if selected_raw:
        selected_lead = unpack_audit(selected_raw, include_pitches=True)

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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    unpacked_lead = unpack_audit(lead)
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    unpacked_lead = unpack_audit(lead)
    history = database.get_lead_outreach_history(lead_id, db_path=db_path)

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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
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
    database.update_lead_fields(lead_id, {"status_reason": f"Pitch Copied: {label}"}, db_path=db_path)
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
    message_text: Optional[str] = Form(None)
):
    """
    Inline action handler for daily queue verification card and contacted lead drawer.
    Supports staying on current lead to review/edit, drawer inline update, or advancing to next lead.
    Tracks and records the exact active 1-of-8 strategy key plus final edited textarea value.
    """
    cfg = load_config()
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
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
        pitch_label = pitch_map.get(pitch_type) if pitch_type else None
        if not pitch_label:
            current_l = database.get_lead_by_id(lead_id, db_path=db_path)
            if current_l:
                unp = unpack_audit(current_l, include_pitches=True)
                rec = unp.get("pitches", {}).get("recommended", "naive_curiosity")
                pitch_label = pitch_map.get(rec, "Curiosity")
            else:
                pitch_label = "Curiosity"

        # Record exact strategy key and user edited textarea text if provided
        strategy_key = pitch_type or "naive_curiosity"
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
        deferred_until = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
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
        unpacked_current = unpack_audit(current_lead, include_pitches=True)
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
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

    # If user selected to stay on the lead in card view (to review details, edit data, or observe changes)
    if stay == "true":
        current_lead = database.get_lead_by_id(lead_id, db_path=db_path)
        unpacked_current = unpack_audit(current_lead, include_pitches=True)
        queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
        stats = database.get_pipeline_stats(db_path=db_path)
        raw_leads = queue_data.get("leads", [])
        next_leads = [unpack_audit(l, include_pitches=False) for l in raw_leads]

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
                "stats": stats
            }
        )

    # Otherwise fetch next lead in line and advance queue
    queue_data = database.get_daily_queue(daily_queue_size=daily_size, db_path=db_path)
    stats = database.get_pipeline_stats(db_path=db_path)
    raw_leads = queue_data.get("leads", [])
    next_leads = [unpack_audit(l, include_pitches=False) for l in raw_leads]
    next_lead = unpack_audit(raw_leads[0], include_pitches=True) if raw_leads else None

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
    source: Optional[str] = Form(None)
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
        message_text=message_text
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    fields_to_update = {}
    if business_name is not None and business_name.strip():
        fields_to_update["business_name"] = business_name.strip()
    if website_url is not None:
        fields_to_update["website_url"] = website_url.strip() if website_url.strip() else None
    if phone is not None:
        fields_to_update["phone"] = phone.strip() if phone.strip() else None
    if clean_phone is not None:
        fields_to_update["clean_phone"] = clean_phone.strip() if clean_phone.strip() else None
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
    unpacked_lead = unpack_audit(updated)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
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
    stats = database.get_pipeline_stats(db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_card.html",
        context={
            "lead": unpacked_lead,
            "lead_updated": True,
            "status_message": "✓ Lead details updated and saved to SQLite!",
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    daily_size = cfg.get("queue", {}).get("daily_queue_size", 35)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    current_wa = lead.get("whatsapp_url")
    if current_wa and current_wa.strip():
        # Toggle OFF: remove WhatsApp channel
        new_wa = None
        action_msg = "WhatsApp removed as channel"
    else:
        # Toggle ON: construct direct WhatsApp link using phone digits
        phone = lead.get("clean_phone") or lead.get("phone")
        if phone:
            digits = re.sub(r"\D", "", phone)
            if len(digits) == 10:
                digits = f"1{digits}"
            new_wa = f"https://wa.me/{digits}"
        else:
            new_wa = "https://wa.me/"
        action_msg = "WhatsApp added as channel"

    database.update_lead_fields(lead_id, {"whatsapp_url": new_wa}, db_path=db_path)
    updated_lead = database.get_lead_by_id(lead_id, db_path=db_path)
    unpacked = unpack_audit(updated_lead)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
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
    next_leads = [unpack_audit(l, include_pitches=False) for l in raw_leads]
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
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
    unpacked = unpack_audit(updated_lead)

    if source == "drawer":
        history = database.get_lead_outreach_history(lead_id, db_path=db_path)
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
    next_leads = [unpack_audit(l, include_pitches=False) for l in raw_leads]
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
    database.delete_outreach_log(log_id, db_path=db_path)

    lead = database.get_lead_by_id(lead_id, db_path=db_path)
    unpacked = unpack_audit(lead)
    history = database.get_lead_outreach_history(lead_id, db_path=db_path)

    return templates.TemplateResponse(
        request=request,
        name="partials/lead_drawer.html",
        context={
            "lead": unpacked,
            "history": history,
            "status_saved": True,
            "status_message": "✓ Outreach entry removed"
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
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
    unpacked_leads = [unpack_audit(l, include_pitches=False) for l in contacted_leads]
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
    db_path = cfg.get("app", {}).get("db_path", "lead_pipeline.db")
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
    unpacked_leads = [unpack_audit(l, include_pitches=False) for l in leads]
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
