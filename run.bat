@echo off
chcp 65001 >nul 2>&1
setlocal

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM === First-time setup: ensure venv and dependencies ===
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    where uv >nul 2>&1
    if not errorlevel 1 (
        uv sync
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python 3.10+ not found. Install Python or uv first.
            pause
            exit /b 1
        )
        python -m venv .venv
        .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
    )
    if errorlevel 1 (
        echo [ERROR] Setup failed. Please run manually.
        pause
        exit /b 1
    )
)

REM === Launch once (no restart loop; the app has its own single-instance lock) ===
start "" ".venv\Scripts\pythonw.exe" "%SCRIPT_DIR%floating_window.py"
exit /b 0
