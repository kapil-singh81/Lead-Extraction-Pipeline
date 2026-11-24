# Lead Extraction & Mobile Site Auditing Pipeline

A zero-cloud, deterministic micro-CRM and automated lead intelligence pipeline for local service contractors.

Harvests Google Maps listings, performs deep headless mobile audits (real load times, TTFB, DOM node bloat, tap-to-call links, JSON-LD social/email extraction), deterministically qualifies leads, and serves an interactive 1-click verification cockpit.

---

## Architecture Overview

```
Google Maps Harvester (Playwright)
          ↓
Mobile Conversion Auditor (Playwright Mobile Context)
          ↓
Deterministic Qualification Gate & Scoring (classifier.py)
          ↓
Local SQLite WAL Database (data/lead_pipeline.db)
          ↓
FastAPI + HTMX Verification Cockpit (dashboard.py)
```

---

## Directory Structure

```
lead_extraction_pipeline/
├── main.py                     # CLI pipeline runner (harvest + audit)
├── dashboard.py                # FastAPI + HTMX Cockpit (http://localhost:8000)
├── config.json                 # Thresholds, rankings, and operational settings
├── requirements.txt            # Python dependencies
├── .gitignore                  # Git ignore rules (includes data/)
├── README.md                   # Documentation & quickstart guide
├── setup.bat                   # Environment & dependency setup
├── start.bat                   # Interactive pipeline runner
├── dashboard.bat               # Cockpit launcher
│
├── core/                       # Core pipeline package
│   ├── __init__.py             # Public package exports
│   ├── auditor.py              # Mobile site auditor & telemetry
│   ├── c1_scraper.py           # Google Maps extractor
│   ├── classifier.py           # Lead qualification & scoring
│   ├── database.py             # SQLite WAL access layer
│   └── pitch_generator.py      # 8-approach outreach copy generator
│
├── templates/                  # Jinja2 templates (dashboard UI)
├── static/                     # Static CSS & vendored HTMX
├── scripts/                    # Maintenance & utility scripts
├── tests/                      # Automated pytest suite (30 unit & integration tests)
└── data/                       # Local SQLite DB & Chrome profiles (git-ignored)
```

---

## Quickstart

### 1. Setup Environment
```powershell
# Automated setup (Windows)
setup.bat

# Or manual setup:
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Run Harvester & Auditor Pipeline
```powershell
# Run using default queries from config.json
python main.py

# Or target a specific query and country
python main.py --query "commercial roofer Dallas TX" --country US

# Or re-audit existing database leads without scraping new ones
python main.py --skip-scrape --re-audit
```

### 3. Launch Verification Cockpit
```powershell
# Start local FastAPI web cockpit on http://localhost:8000
python dashboard.py
# Or double-click: dashboard.bat
```

### 4. Run Test Suite
```powershell
python -m pytest tests/ -v
```
