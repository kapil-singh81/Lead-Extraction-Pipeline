@echo off
setlocal EnableDelayedExpansion
title Lead Extraction Pipeline - Runner

:: Ensure virtual environment exists
if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment 'venv' not found.
    echo Please run 'setup.bat' first to initialize the environment.
    pause
    exit /b 1
)

:: Activate environment
call venv\Scripts\activate.bat

cls
echo ==============================================================================
echo                    LEAD EXTRACTION PIPELINE - RUNNER
echo ==============================================================================
echo.

:prompt_query
echo Enter the Google Maps search query you want to harvest.
echo Examples:
echo   - commercial roofer Dallas TX
echo   - cosmetic dentist Miami FL
echo   - family lawyer Austin TX
echo.
set "USER_QUERY="
set /p "USER_QUERY=Search Query (or press Enter to use queries from config.json): "

if "!USER_QUERY!"=="" (
    echo.
    echo [i] No custom query entered. The pipeline will run default queries from config.json.
    set "RUN_CUSTOM=0"
) else (
    set "RUN_CUSTOM=1"
    echo.
    set "USER_COUNTRY=US"
    set /p "USER_COUNTRY=Target Country Code [Press Enter for default: US]: "
)

echo.
echo ==============================================================================
if "!RUN_CUSTOM!"=="1" (
    echo  Target Query   : !USER_QUERY!
    echo  Target Country : !USER_COUNTRY!
) else (
    echo  Target Mode    : Using default query list from config.json
)
echo ==============================================================================
echo.
echo Starting pipeline in 3 seconds... Press Ctrl+C to cancel.
timeout /t 3 >nul

:: Execute the pipeline
if "!RUN_CUSTOM!"=="1" (
    python main.py --query "!USER_QUERY!" --country "!USER_COUNTRY!"
) else (
    python main.py
)

echo.
echo ==============================================================================
echo                        PIPELINE EXECUTION FINISHED
echo ==============================================================================
echo.
pause

