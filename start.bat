@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Lead Extraction Pipeline - Runner

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not switch working directory to "%~dp0".
    pause
    exit /b 1
)

set "VENV_PY=%CD%\venv\Scripts\python.exe"

:: Ensure local virtual environment exists and belongs to this device/folder
if not exist "%VENV_PY%" goto venv_missing
if not exist "venv\Scripts\activate.bat" goto venv_missing

"%VENV_PY%" -c "import os, pathlib, sys; v=pathlib.Path('venv'); act=pathlib.Path('venv/Scripts/activate.bat').read_text(encoding='utf-8', errors='ignore'); ok=os.path.samefile(sys.prefix, v) and (os.path.normcase(os.path.abspath(v)) in os.path.normcase(act)); import playwright, fastapi; raise SystemExit(0 if ok else 1)" >nul 2>&1
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

:: If CLI flags were passed directly (e.g. start.bat --skip-scrape --re-audit), forward them
if not "%~1"=="" goto forward_cli

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
    if "!USER_COUNTRY!"=="" set "USER_COUNTRY=US"
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

:: Execute the pipeline using the local venv Python directly
if "!RUN_CUSTOM!"=="1" (
    "%VENV_PY%" main.py --query "!USER_QUERY!" --country "!USER_COUNTRY!"
) else (
    "%VENV_PY%" main.py
)
set "PIPE_EXIT=!errorlevel!"
goto finished

:forward_cli
"%VENV_PY%" main.py %*
set "PIPE_EXIT=!errorlevel!"

:finished
echo.
if not "!PIPE_EXIT!"=="0" (
    echo [ERROR] Pipeline exited with code !PIPE_EXIT!. Check logs above for details.
)
echo ==============================================================================
echo                        PIPELINE EXECUTION FINISHED
echo ==============================================================================
echo.
pause
popd
exit /b !PIPE_EXIT!
