@echo off
setlocal EnableDelayedExpansion
title Lead Extraction Pipeline - Setup & Configuration Wizard

echo ==============================================================================
echo              LEAD EXTRACTION PIPELINE - SYSTEM SETUP & CONFIG
echo ==============================================================================
echo.

:: 1. CHECK PYTHON INSTALLATION
echo [*] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to your system PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check the box "Add Python to PATH" during installation.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [+] Found Python version: !PY_VER!

:: 2. CREATE VIRTUAL ENVIRONMENT
if not exist "venv" (
    echo [*] Creating virtual environment (venv)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [+] Virtual environment created.
) else (
    echo [+] Virtual environment (venv) already exists.
)

:: 3. ACTIVATE & INSTALL DEPENDENCIES
echo [*] Activating virtual environment...
call venv\Scripts\activate.bat

echo [*] Upgrading pip...
python -m pip install --upgrade pip --quiet

echo [*] Installing requirements (playwright, gspread, etc.)...
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install packages from requirements.txt.
    pause
    exit /b 1
)
echo [+] Dependencies installed successfully.

:: 4. INSTALL PLAYWRIGHT CHROMIUM
echo [*] Checking Playwright Chromium browser binary...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Playwright browser.
    pause
    exit /b 1
)
echo [+] Playwright Chromium ready.

:: 5. CHECK GOOGLE CLOUD CREDENTIALS
echo [*] Checking Google Cloud credentials (credentials.json)...
if not exist "credentials.json" (
    echo.
    echo [!] WARNING: 'credentials.json' was NOT found in this folder.
    echo     Google Sheets synchronization requires a free Google Cloud Service Account key.
    echo.
    echo     How to fix:
    echo     1. Go to Google Cloud Console -^> IAM -^> Service Accounts.
    echo     2. Create a service account and generate a JSON key.
    echo     3. Rename the downloaded key file to 'credentials.json' and place it here.
    echo     4. Share your Google Sheet with the service account email as "Editor".
    echo.
) else (
    echo [+] Google Cloud credentials file found: credentials.json
)

:: 6. DATABASE CHECK
if not exist "lead_pipeline.db" (
    echo [*] Initializing local database (lead_pipeline.db)...
    python -c "import database, json; cfg=json.load(open('config.json')); database.init_db(cfg['app']['db_path'])"
    echo [+] SQLite database initialized.
) else (
    echo [+] SQLite database already present: lead_pipeline.db
)

echo.
echo ==============================================================================
echo                         SETUP COMPLETED SUCCESSFULLY
echo ==============================================================================
echo.
echo Select an option:
echo   [1] View plain-English explanation of all config.json settings
echo   [2] Test Google Sheets and Database connectivity
echo   [3] Exit Setup
echo.
set /p MENU_CHOICE="Enter your choice (1, 2, or 3): "

if "%MENU_CHOICE%"=="1" goto explain_config
if "%MENU_CHOICE%"=="2" goto test_connections
goto end_setup

:explain_config
cls
echo ==============================================================================
echo             PLAIN-ENGLISH GUIDE TO ALL 'config.json' SETTINGS
echo ==============================================================================
echo.
echo 1. [app]
echo    - "db_path" (Default: lead_pipeline.db):
echo        The local SQLite database storing all scraped businesses, deduplication
echo        hashes, and speed audit records. Completely free and runs locally.
echo    - "target_country" (Default: "US"):
echo        The default country code used to format raw phone numbers into standard
echo        E.164 international format (+1 ...) and parse geographic location.
echo    - "log_level" (Default: "INFO"):
echo        Controls terminal verbosity (DEBUG for verbose, INFO for standard).
echo.
echo 2. [scraper_c1] (Google Maps Harvester)
echo    - "browser_channel": "chrome" uses your installed Google Chrome browser.
echo    - "headless": false opens a visible browser so Google Maps treats you like
echo      a real person on a residential connection. No expensive proxies needed.
echo    - "user_data_dir" (Default: ./chrome_lead_profile):
echo      A dedicated Chrome profile folder storing cookies and session history.
echo      This keeps your automation trusted by Google across repeated runs.
echo    - "target_queries":
echo      Default list of search terms (e.g. "commercial roofer Dallas TX").
echo    - "max_results_per_query" (Default: 60):
echo      Maximum Google Maps listings harvested per search term.
echo    - "scroll_delay_min_ms" / "scroll_delay_max_ms" (Default: 1200 - 2800):
echo      Humanized random delay between scrolling listing results.
echo.
echo 3. [auditor] (Technical Website Auditor & Contact Finder)
echo    - "concurrency_limit" (Default: 5):
echo      Audits up to 5 websites at the same time in parallel headless tabs.
echo    - "page_timeout_ms" (Default: 15000 / 15 seconds):
echo      Maximum time allowed for a website to respond before timing out.
echo    - "thresholds":
echo        * "max_load_time_seconds" (Default: 4.5): Sites slower than 4.5s fail.
echo        * "max_ttfb_ms" (Default: 1500): Server response time. Over 1.5s fails.
echo        * "max_page_weight_mb" (Default: 5.0): Heavy sites over 5MB fail.
echo        * "max_dom_nodes" (Default: 1800): Bloated HTML (>1800 tags) fails.
echo    - "mobile_viewport": Tests if website overflows on iPhone screen (390x844).
echo    - "decision_maker_keywords": Keywords to identify Owner, CEO, Founder.
echo.
echo 4. [sheets] (Google Sheets Sync & Routing Engine)
echo    - "spreadsheet_id":
echo        The unique ID in your Google Sheet URL (docs.google.com/spreadsheets/d/.../edit).
echo    - "credentials_file": "credentials.json" (Google Service Account key).
echo    - "tabs":
echo        * "today": Temporary intake buffer for newly audited leads.
echo        * "outreach_ready": High-value leads (flawed site + verified DM contact).
echo        * "missing_info": Flawed sites or no-site leads missing DM contact.
echo        * "disqualified": Healthy/fast sites or dead domains.
echo        * "archive": Master ledger storing all leads deduplicated by Lead UUID.
echo.
echo ==============================================================================
pause
goto end_setup

:test_connections
echo.
echo [*] Testing database and Google Sheets connectivity...
python -c "
import json, sqlite3, gspread
cfg = json.load(open('config.json'))
conn = sqlite3.connect(cfg['app']['db_path'])
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM leads')
print(f'[DB] SQLite connection successful. Current leads in DB: {cur.fetchone()[0]}')
try:
    gc = gspread.service_account(filename=cfg['sheets']['credentials_file'])
    sp = gc.open_by_key(cfg['sheets']['spreadsheet_id'])
    print(f'[SHEETS] Google Sheets connected successfully: \"{sp.title}\"')
except Exception as e:
    print(f'[SHEETS ERROR] Could not connect to Google Sheets: {e}')
"
echo.
pause

:end_setup
echo.
echo Setup is complete! You can now run 'start.bat' anytime to extract leads.
pause

