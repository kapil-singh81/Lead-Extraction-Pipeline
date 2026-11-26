@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Lead Extraction Pipeline - Setup and Configuration Wizard

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not switch working directory to "%~dp0".
    pause
    exit /b 1
)

set "NON_INTERACTIVE=0"
set "SEED_NANPA=0"
for %%A in (%*) do (
    if /i "%%~A"=="--non-interactive" set "NON_INTERACTIVE=1"
    if /i "%%~A"=="--ci" set "NON_INTERACTIVE=1"
    if /i "%%~A"=="-y" set "NON_INTERACTIVE=1"
    if /i "%%~A"=="--seed-nanpa" set "SEED_NANPA=1"
)

echo ==============================================================================
echo              LEAD EXTRACTION PIPELINE - SYSTEM SETUP ^& CONFIG
echo ==============================================================================
echo [i] Project directory: %CD%
echo.

if not exist "requirements.txt" goto missing_project_files
if not exist "config.json" goto missing_project_files
goto find_python

:missing_project_files
echo [ERROR] Missing requirements.txt or config.json in "%CD%".
echo Please ensure you extracted or cloned the complete repository.
goto fail_exit

:: 1. FIND PYTHON 3.10+ (Handles PATH, py launcher, custom installs, and skips Store stub)
:find_python
echo [*] Checking Python 3.10+ installation...
set "PY_EXE="
set "PY_VER="
set "OLD_PY_VER="

call :probe_python py -3
if defined PY_EXE goto python_found

call :probe_python python
if defined PY_EXE goto python_found

call :probe_python python3
if defined PY_EXE goto python_found

for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" (
        call :probe_python "%%~fD\python.exe"
        if defined PY_EXE goto python_found
    )
)
for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%~fD\python.exe" (
        call :probe_python "%%~fD\python.exe"
        if defined PY_EXE goto python_found
    )
)

echo [ERROR] Python 3.10+ was not found on this device.
if defined OLD_PY_VER echo [WARN] Detected incompatible Python version: !OLD_PY_VER! ^(Python 3.10 or newer is required^).
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo During installation, check "Add Python to PATH" ^(or install the py launcher^).
goto fail_exit

:python_found
echo [+] Found Python !PY_VER! at: !PY_EXE!

:: 2. CREATE OR VALIDATE VIRTUAL ENVIRONMENT
set "VENV_DIR=%CD%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" goto check_partial_venv

"%VENV_PY%" -c "import os, pathlib, sys; v=pathlib.Path('venv'); act=pathlib.Path('venv/Scripts/activate.bat').read_text(encoding='utf-8', errors='ignore'); ok=os.path.samefile(sys.prefix, v) and (os.path.normcase(os.path.abspath(v)) in os.path.normcase(act)); raise SystemExit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 goto rebuild_stale_venv

"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 goto rebuild_stale_venv

echo [+] Valid local virtual environment already exists.
goto venv_ready

:check_partial_venv
if not exist "%VENV_DIR%" goto create_venv
echo [WARN] Incomplete 'venv' folder detected. Recreating clean virtual environment...
rmdir /s /q "%VENV_DIR%"
goto create_venv

:rebuild_stale_venv
echo [WARN] Existing 'venv' belongs to another device/path or is corrupted.
echo [*] Removing stale 'venv' and creating a fresh one for this device...
rmdir /s /q "%VENV_DIR%"
if not exist "%VENV_DIR%" goto create_venv
echo [ERROR] Could not remove old 'venv'. Close any terminals or Python processes using it and retry.
goto fail_exit

:create_venv
echo [*] Creating virtual environment...
"%PY_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 goto venv_create_failed
echo [+] Virtual environment created at: %VENV_DIR%

:venv_ready
if not exist "venv\Scripts\activate.bat" goto venv_missing_activate

:: 3. ACTIVATE & INSTALL DEPENDENCIES
echo [*] Activating virtual environment...
call "venv\Scripts\activate.bat"

echo [*] Upgrading pip...
"%VENV_PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
if errorlevel 1 echo [WARN] pip self-upgrade skipped or failed; continuing with existing pip.

echo [*] Installing requirements from requirements.txt...
"%VENV_PY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto pip_install_failed
echo [+] Dependencies installed successfully.

:: 4. INSTALL PLAYWRIGHT CHROMIUM
echo [*] Installing/verifying Playwright Chromium browser binary...
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 goto playwright_failed
echo [+] Playwright Chromium ready.

:: 5. DATABASE INITIALIZATION
echo [*] Initializing local SQLite database...
"%VENV_PY%" -c "import json; from core import database; cfg=json.load(open('config.json', encoding='utf-8')); db=cfg.get('app', {}).get('db_path', 'data/lead_pipeline.db'); database.init_db(db); print(f'[+] SQLite database ready: {db}')"
if errorlevel 1 goto db_init_failed

if "%SEED_NANPA%"=="1" (
    echo [*] Seeding offline FCC/NANPA phone prefix table...
    "%VENV_PY%" scripts\seed_nanpa.py
)

"%VENV_PY%" -c "import json; from core import database; cfg=json.load(open('config.json', encoding='utf-8')); conn=database.get_connection(cfg.get('app', {}).get('db_path')); n=conn.execute('SELECT COUNT(*) FROM phone_prefixes').fetchone()[0]; conn.close(); print('[i] Offline NANPA phone prefix table is empty (0 rows).\n    You can seed Mobile/Landline detection anytime via option [3] below or run.bat nanpa.' if n == 0 else f'[+] Offline NANPA phone prefix table loaded ({n} prefixes).')"

echo.
echo ==============================================================================
echo                         SETUP COMPLETED SUCCESSFULLY
echo ==============================================================================
echo.
if "%NON_INTERACTIVE%"=="1" goto success_exit

:setup_menu
echo Select an option:
echo   [1] View plain-English guide to config.json settings
echo   [2] Test database ^& Playwright environment health
echo   [3] Seed offline FCC/NANPA phone line-type database ^(requires internet^)
echo   [4] Exit Setup
echo.
set "MENU_CHOICE="
set /p "MENU_CHOICE=Enter your choice (1, 2, 3, or 4): "

if "%MENU_CHOICE%"=="1" goto explain_config
if "%MENU_CHOICE%"=="2" goto test_connections
if "%MENU_CHOICE%"=="3" goto run_seed_nanpa
goto end_setup

:explain_config
cls
echo ==============================================================================
echo             PLAIN-ENGLISH GUIDE TO ALL 'config.json' SETTINGS
echo ==============================================================================
echo.
echo 1. [app]
echo    - "db_path" ^(Default: data/lead_pipeline.db^):
echo        Local SQLite WAL database storing scraped businesses, deduplication
echo        hashes, audit telemetry, and outreach logs. Runs 100%% locally.
echo    - "target_country" ^(Default: "US"^):
echo        Default country code used to format phone numbers and parse queries.
echo    - "log_level" ^(Default: "INFO"^):
echo        Terminal logging verbosity ^(DEBUG or INFO^).
echo.
echo 2. [scraper_c1] ^(Google Maps Harvester^)
echo    - "browser_channel": Uses local Google Chrome if installed, or
echo      automatically falls back to Playwright's bundled Chromium on devices
echo      without Chrome.
echo    - "headless": true/false ^(false opens a visible browser window^).
echo    - "user_data_dir" ^(Default: data/chrome_lead_profile^):
echo        Local browser profile directory ^(created per device, git-ignored^).
echo    - "target_queries": Default list of Google Maps search terms.
echo    - "max_results_per_query" ^(Default: 50^): Max listings per query.
echo.
echo 3. [auditor] ^(Mobile Website Auditor ^& Contact Finder^)
echo    - "concurrency_limit" ^(Default: 2^): Parallel headless mobile audit tabs.
echo    - "page_timeout_ms" ^(Default: 25000 / 25 seconds^): Page load timeout.
echo    - "thresholds":
echo        * "max_load_time_seconds" ^(Default: 6.0^): Sites slower than 6.0s fail.
echo        * "max_ttfb_ms" ^(Default: 2500^): Server response over 2.5s fails.
echo        * "max_page_weight_mb" ^(Default: 5.0^): Heavy sites over 5MB fail.
echo        * "max_dom_nodes" ^(Default: 1800^): Bloated DOM ^(^>1800 nodes^) fails.
echo    - "mobile_viewport": iPhone viewport ^(390x844^) for overflow/tap checks.
echo.
echo 4. [queue] ^(Daily Verification Queue ^& Ranking^)
echo    - "daily_queue_size" ^(Default: 35^): Max leads surfaced in daily queue.
echo    - "ranking_weights": Priority weights for detected flaws and contacts.
echo.
echo ==============================================================================
pause
goto setup_menu

:test_connections
echo.
echo [*] Running system, database, and browser checks...
"%VENV_PY%" -c "import json, fastapi, uvicorn, jinja2, httpx, playwright; from core import database; cfg=json.load(open('config.json', encoding='utf-8')); db=cfg.get('app', {}).get('db_path', 'data/lead_pipeline.db'); conn=database.get_connection(db); leads_n=conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0]; nanpa_n=conn.execute('SELECT COUNT(*) FROM phone_prefixes').fetchone()[0]; conn.close(); print(f'[DB] Connected to {db} | Leads: {leads_n} | NANPA prefixes: {nanpa_n}'); print('[PKG] FastAPI, Uvicorn, Jinja2, HTTPX, Playwright imports OK.')"
echo.
pause
goto setup_menu

:run_seed_nanpa
echo.
echo [*] Downloading and seeding FCC/NANPA prefix table...
"%VENV_PY%" scripts\seed_nanpa.py
echo.
pause
goto setup_menu

:end_setup
echo.
echo Setup is complete!
echo   - Run 'run.bat' for the unified launcher menu ^(pipeline, dashboard, tests^).
echo   - Run 'start.bat' to harvest and audit new leads directly.
echo   - Run 'dashboard.bat' to open the Daily Verification Cockpit ^(http://127.0.0.1:8000^).
pause

:success_exit
popd
exit /b 0

:venv_create_failed
echo [ERROR] Failed to create virtual environment.
goto fail_exit

:venv_missing_activate
echo [ERROR] Virtual environment activation script is missing after creation.
goto fail_exit

:pip_install_failed
echo [ERROR] Failed to install packages from requirements.txt.
echo Check your internet connection or proxy settings and re-run setup.bat.
goto fail_exit

:playwright_failed
echo [ERROR] Failed to install Playwright Chromium browser.
echo Check your internet connection or firewall and re-run setup.bat.
goto fail_exit

:db_init_failed
echo [ERROR] Failed to initialize SQLite database.
goto fail_exit

:fail_exit
echo.
echo Setup did NOT complete. Fix the error above and run 'setup.bat' again.
if "%NON_INTERACTIVE%"=="0" pause
popd
exit /b 1

:: --- Helper Subroutine: Probe a Python executable or launcher ---
:probe_python
set "CAND=%~1"
set "CAND_ARG=%~2"
set "PROBE_OUT=%TEMP%\lep_py_probe_%RANDOM%.txt"
if not defined CAND_ARG goto probe_single

%CAND% %CAND_ARG% -c "import sys; raise SystemExit(0 if (sys.version_info >= (3, 10) and 'WindowsApps' not in sys.executable) else 1)" >nul 2>&1
if errorlevel 1 goto probe_multi_old
%CAND% %CAND_ARG% -c "import sys; print('PY_EXE=' + sys.executable); print('PY_VER=' + '.'.join(map(str, sys.version_info[:3])))" > "%PROBE_OUT%" 2>nul
for /f "usebackq tokens=1* delims==" %%a in ("%PROBE_OUT%") do set "%%a=%%b"
del "%PROBE_OUT%" >nul 2>&1
exit /b 0

:probe_multi_old
%CAND% %CAND_ARG% -c "import sys; print('' if 'WindowsApps' in sys.executable else '.'.join(map(str, sys.version_info[:3])))" > "%PROBE_OUT%" 2>nul
if exist "%PROBE_OUT%" (
    for /f "usebackq delims=" %%v in ("%PROBE_OUT%") do if not defined OLD_PY_VER set "OLD_PY_VER=%%v"
    del "%PROBE_OUT%" >nul 2>&1
)
exit /b 0

:probe_single
"%CAND%" -c "import sys; raise SystemExit(0 if (sys.version_info >= (3, 10) and 'WindowsApps' not in sys.executable) else 1)" >nul 2>&1
if errorlevel 1 goto probe_single_old
"%CAND%" -c "import sys; print('PY_EXE=' + sys.executable); print('PY_VER=' + '.'.join(map(str, sys.version_info[:3])))" > "%PROBE_OUT%" 2>nul
for /f "usebackq tokens=1* delims==" %%a in ("%PROBE_OUT%") do set "%%a=%%b"
del "%PROBE_OUT%" >nul 2>&1
exit /b 0

:probe_single_old
"%CAND%" -c "import sys; print('' if 'WindowsApps' in sys.executable else '.'.join(map(str, sys.version_info[:3])))" > "%PROBE_OUT%" 2>nul
if exist "%PROBE_OUT%" (
    for /f "usebackq delims=" %%v in ("%PROBE_OUT%") do if not defined OLD_PY_VER set "OLD_PY_VER=%%v"
    del "%PROBE_OUT%" >nul 2>&1
)
exit /b 0
