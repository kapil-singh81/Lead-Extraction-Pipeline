# Lead Extraction & Mobile Site Auditing Pipeline

A zero-cloud, deterministic micro-CRM and automated lead intelligence pipeline for local service businesses.

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
├── dashboard.py                # FastAPI + HTMX Cockpit (http://127.0.0.1:8000)
├── config.json                 # Thresholds, rankings, and operational settings
├── requirements.txt            # Python runtime & test dependencies
├── .gitignore                  # Git ignore rules (includes data/ and venv/)
├── README.md                   # Documentation & quickstart guide
├── setup.bat                   # Windows environment & dependency setup
├── run.bat                     # Windows unified launcher menu & CLI router
├── start.bat                   # Windows interactive pipeline runner
├── dashboard.bat               # Windows cockpit launcher
├── setup.sh                    # macOS / Linux environment setup
├── run.sh                      # macOS / Linux unified launcher
│
├── core/                       # Core pipeline package
│   ├── __init__.py             # Public package exports
│   ├── auditor.py              # Mobile site auditor & telemetry
│   ├── c1_scraper.py           # Google Maps extractor (Chrome + Chromium fallback)
│   ├── classifier.py           # Lead qualification & scoring
│   ├── database.py             # SQLite WAL access layer
│   └── pitch_generator.py      # 8-approach outreach copy generator
│
├── templates/                  # Jinja2 templates (dashboard UI)
├── static/                     # Static CSS & vendored HTMX
├── scripts/                    # Maintenance & utility scripts
├── tests/                      # Automated pytest suite
└── data/                       # Local SQLite DB & Chrome profiles (git-ignored)
```

---

## Prerequisites

- **Python 3.10+** (on Windows, ensure **"Add Python to PATH"** or the `py` launcher is installed)
- **Internet connection** during initial setup (`pip` packages + Playwright Chromium download)
- **Google Chrome** *(optional)* — if Chrome is not installed on a device, the scraper automatically falls back to Playwright's bundled Chromium.

---

## Quickstart

### Windows (`.bat` launchers)

1. **First-time Setup** (double-click `setup.bat` or run from terminal):
   ```cmd
   setup.bat
   ```
   > `setup.bat` automatically anchors to the project folder, detects Python 3.10+, recreates `venv/` if copied from another PC/user, installs dependencies, downloads Playwright Chromium, and initializes `data/lead_pipeline.db`.

2. **Launch Everything via `run.bat`** (or use `start.bat` / `dashboard.bat` directly):
   ```cmd
   run.bat               # Interactive launcher menu (Pipeline, Cockpit, NANPA, Tests)
   run.bat dashboard     # Open Verification Cockpit (http://127.0.0.1:8000)
   run.bat start         # Interactive Maps harvest + mobile audit (same as start.bat)
   run.bat test          # Run automated test suite
   ```

### macOS / Linux (`.sh` launchers)

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

### Manual CLI Commands

```powershell
# Activate virtual environment (Windows) / source venv/bin/activate (macOS/Linux)
venv\Scripts\activate

# Run using default queries from config.json
python main.py

# Target a specific query and country
python main.py --query "commercial roofer Dallas TX" --country US

# Re-audit existing database leads without scraping new ones
python main.py --skip-scrape --re-audit

# Seed free offline FCC/NANPA mobile vs. landline phone classification
python scripts/seed_nanpa.py

# Launch local FastAPI web cockpit on http://127.0.0.1:8000
python dashboard.py --open-browser

# Run automated test suite
python -m pytest tests/ -v
```

---

## Shipping / Moving to Another Device or User

When sharing or deploying this project to another machine or teammate:

1. **Preferred**: Clone via Git (`git clone ...`), then run `setup.bat` (Windows) or `./setup.sh` (macOS/Linux).
2. **If sharing as a ZIP/folder**: Exclude `venv/`, `data/`, `__pycache__/`, `.pytest_cache/`, and `*.db*` files so you don't ship machine-specific virtual environments, browser session locks, or local databases.
3. If `venv/` from another device *was* accidentally included, `setup.bat` / `setup.sh` detects the mismatched path and recreates a clean local `venv/` automatically.
