@echo off
REM ── J.A.R.V.I.S. one-time setup for Windows ──
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
echo Creating virtual environment...
%PY% -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || (pause & exit /b 1)
if not exist .env copy .env.example .env >nul
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
echo.
echo  Setup complete.
echo  1) Open .env in Notepad and paste your GEMINI_API_KEY and an access code.
echo  2) Put your Google OAuth file at data\credentials.json (see README).
echo  3) Double-click the JARVIS icon on your desktop.
echo.
notepad .env
pause
