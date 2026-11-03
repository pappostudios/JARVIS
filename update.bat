@echo off
REM ── Update JARVIS to the latest version. Keeps your .env, data and Google sign-in. ──
cd /d "%~dp0"
title Updating J.A.R.V.I.S.
if not exist .venv\Scripts\python.exe (echo Run install.bat first. & pause & exit /b 1)
where git >nul 2>nul
if errorlevel 1 (
  echo Git is needed for automatic updates. Installing it now - accept any prompts...
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  echo.
  echo Git installed. Close this window and double-click update.bat again.
  pause
  exit /b
)
.venv\Scripts\python.exe -m jarvis.updater
if errorlevel 1 (pause & exit /b 1)
echo.
echo Starting JARVIS...
start "" ".venv\Scripts\pythonw.exe" desktop.py --after-update
timeout /t 3 >nul
