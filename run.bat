@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM === First-time setup: ensure venv and dependencies ===
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    where uv >nul 2>&1
    if not errorlevel 1 (
        uv venv --clear
        uv sync
    ) else (
        python -m venv .venv
        .venv\Scripts\pip install --quiet PySide6 eltdx
    )
    if errorlevel 1 (
        echo [ERROR] Setup failed. Please run manually.
        pause
        exit /b 1
    )
)

REM === Launch with pythonw (no console window) ===
:loop
start "" /min ".venv\Scripts\pythonw.exe" "%SCRIPT_DIR%floating_window.py"
timeout /t 5 /nobreak >nul
goto loop
