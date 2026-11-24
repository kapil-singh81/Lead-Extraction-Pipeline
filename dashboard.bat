@echo off
setlocal EnableDelayedExpansion
title Lead Extraction Pipeline - Verification Cockpit

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment 'venv' not found.
    echo Please run 'setup.bat' first to initialize the environment.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

cls
echo ==============================================================================
echo                LEAD EXTRACTION PIPELINE - VERIFICATION COCKPIT
echo ==============================================================================
echo.
echo Launching local FastAPI + HTMX micro-CRM on http://localhost:8000
echo Inspect today's top-ranked leads, verify websites, and log outreach.
echo.
echo Press Ctrl+C in this terminal when you want to stop the server.
echo ==============================================================================
echo.

start "" http://localhost:8000
python dashboard.py
pause
