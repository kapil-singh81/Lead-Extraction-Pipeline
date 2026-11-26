@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Lead Extraction Pipeline - Unified Launcher

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not switch working directory to "%~dp0".
    pause
    exit /b 1
)

set "VENV_PY=%CD%\venv\Scripts\python.exe"

:: Fast CLI dispatch support (e.g. run.bat setup / run.bat dashboard / run.bat test / run.bat --query "...")
if /i "%~1"=="setup" (
    shift
    call "%~dp0setup.bat" %*
    set "EXIT_CODE=!errorlevel!"
    popd
    exit /b !EXIT_CODE!
)

:: Validate virtual environment before running anything else
if not exist "%VENV_PY%" goto venv_missing
if not exist "venv\Scripts\activate.bat" goto venv_missing

"%VENV_PY%" -c "import os, pathlib, sys; v=pathlib.Path('venv'); act=pathlib.Path('venv/Scripts/activate.bat').read_text(encoding='utf-8', errors='ignore'); ok=os.path.samefile(sys.prefix, v) and (os.path.normcase(os.path.abspath(v)) in os.path.normcase(act)); import fastapi, playwright; raise SystemExit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 goto venv_missing
goto venv_ready

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

:venv_ready
call "venv\Scripts\activate.bat"

:: Direct subcommand routing
if "%~1"=="" goto menu
if /i "%~1"=="dashboard" (
    call "%~dp0dashboard.bat"
    set "EXIT_CODE=!errorlevel!"
    popd
    exit /b !EXIT_CODE!
)
if /i "%~1"=="start" (
    shift
    goto run_pipeline_args
)
if /i "%~1"=="pipeline" (
    shift
    goto run_pipeline_args
)
if /i "%~1"=="test" goto run_tests
if /i "%~1"=="tests" goto run_tests
if /i "%~1"=="nanpa" goto run_nanpa
if /i "%~1"=="inspect" goto run_inspect

:: Any other CLI arguments go directly to main.py
"%VENV_PY%" main.py %*
set "EXIT_CODE=!errorlevel!"
popd
exit /b !EXIT_CODE!

:run_pipeline_args
if "%~1"=="" (
    call "%~dp0start.bat"
) else (
    "%VENV_PY%" main.py %*
)
set "EXIT_CODE=!errorlevel!"
popd
exit /b !EXIT_CODE!

:menu
cls
echo ==============================================================================
echo                   LEAD EXTRACTION PIPELINE - MAIN LAUNCHER
echo ==============================================================================
echo [i] Project directory: %CD%
echo.
echo Select an action:
echo   [1] Run Lead Scraper ^& Auditor ^(interactive^)
echo   [2] Open Verification Cockpit Dashboard ^(http://127.0.0.1:8000^)
echo   [3] Run Pipeline ^(default config.json^), then open Dashboard
echo   [4] Seed Offline FCC/NANPA Phone Prefix Database
echo   [5] Run Automated Test Suite ^(pytest^)
echo   [6] Run / Repair Setup ^(setup.bat^)
echo   [7] Exit
echo.
set "RUN_CHOICE="
set /p "RUN_CHOICE=Enter your choice (1-7): "

if "%RUN_CHOICE%"=="1" (
    call "%~dp0start.bat"
    goto menu
)
if "%RUN_CHOICE%"=="2" (
    call "%~dp0dashboard.bat"
    goto menu
)
if "%RUN_CHOICE%"=="3" (
    "%VENV_PY%" main.py
    if errorlevel 1 (
        echo [ERROR] Pipeline failed; skipping dashboard launch.
        pause
        goto menu
    )
    call "%~dp0dashboard.bat"
    goto menu
)
if "%RUN_CHOICE%"=="4" goto run_nanpa
if "%RUN_CHOICE%"=="5" goto run_tests
if "%RUN_CHOICE%"=="6" (
    call "%~dp0setup.bat"
    goto menu
)
popd
exit /b 0

:run_nanpa
echo.
echo [*] Seeding FCC/NANPA phone prefixes...
"%VENV_PY%" scripts\seed_nanpa.py
echo.
pause
if "%~1"=="" goto menu
popd
exit /b 0

:run_inspect
"%VENV_PY%" scripts\inspect_db.py
popd
exit /b 0

:run_tests
echo.
echo [*] Running pytest verification suite...
"%VENV_PY%" -m pytest tests/ -v
set "TEST_EXIT=!errorlevel!"
echo.
if "%~1"=="" (
    pause
    goto menu
)
popd
exit /b !TEST_EXIT!
