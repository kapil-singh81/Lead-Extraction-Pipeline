@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Lead Extraction Pipeline - Verification Cockpit

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not switch working directory to "%~dp0".
    pause
    exit /b 1
)

set "VENV_PY=%CD%\venv\Scripts\python.exe"

if not exist "%VENV_PY%" goto venv_missing
if not exist "venv\Scripts\activate.bat" goto venv_missing

"%VENV_PY%" -c "import os, pathlib, sys; v=pathlib.Path('venv'); act=pathlib.Path('venv/Scripts/activate.bat').read_text(encoding='utf-8', errors='ignore'); ok=os.path.samefile(sys.prefix, v) and (os.path.normcase(os.path.abspath(v)) in os.path.normcase(act)); import fastapi, uvicorn, jinja2; raise SystemExit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 goto venv_missing
goto venv_ok

:venv_missing
echo [ERROR] Virtual environment 'venv' is missing or not configured for this device.
echo Please run 'setup.bat' first to initialize the environment.
echo.
set "AUTO_SETUP="
set /p "AUTO_SETUP=Run setup.bat automatically right now? (Y/N): "
if /i "!AUTO_SETUP!"=="Y" goto invoke_setup
if /i "!AUTO_SETUP!"=="YES" goto invoke_setup
popd
exit /b 1

:invoke_setup
call "%~dp0setup.bat" --non-interactive
if errorlevel 1 (
    popd
    exit /b 1
)
if not exist "%VENV_PY%" (
    popd
    exit /b 1
)

:venv_ok
call "venv\Scripts\activate.bat"

if not defined DASHBOARD_HOST set "DASHBOARD_HOST=127.0.0.1"
if not defined DASHBOARD_PORT set "DASHBOARD_PORT=8000"
set "DASHBOARD_RELOAD=0"

cls
echo ==============================================================================
echo                LEAD EXTRACTION PIPELINE - VERIFICATION COCKPIT
echo ==============================================================================
echo.
echo Launching local FastAPI + HTMX micro-CRM on http://%DASHBOARD_HOST%:%DASHBOARD_PORT%
echo Inspect today's top-ranked leads, verify websites, and log outreach.
echo Browser will open automatically once the server is ready.
echo.
echo Press Ctrl+C in this terminal when you want to stop the server.
echo ==============================================================================
echo.

"%VENV_PY%" dashboard.py --host "%DASHBOARD_HOST%" --port "%DASHBOARD_PORT%" --open-browser --no-reload
set "DASH_EXIT=%errorlevel%"
if not "%DASH_EXIT%"=="0" (
    echo.
    echo [ERROR] Dashboard exited with code %DASH_EXIT%.
    pause
)
popd
exit /b %DASH_EXIT%
