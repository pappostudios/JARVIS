@echo off
REM Starts JARVIS in the background (no console window) and opens the dashboard.
REM Closing the dashboard window does not stop JARVIS - use Settings > "Shut down JARVIS".
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (echo Run install.bat first. & pause & exit /b 1)
start "" ".venv\Scripts\pythonw.exe" desktop.py
