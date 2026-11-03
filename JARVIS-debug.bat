@echo off
REM Same as JARVIS.bat but keeps a console window open so you can see errors.
cd /d "%~dp0"
title JARVIS server (debug) - keep this window open
if not exist .venv\Scripts\python.exe (echo Run install.bat first. & pause & exit /b 1)
.venv\Scripts\python.exe desktop.py
echo.
echo JARVIS has stopped. If something went wrong, the details are above and in data\jarvis.log
pause
