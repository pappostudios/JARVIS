@echo off
cd /d "%~dp0"
title J.A.R.V.I.S.
if not exist .venv\Scripts\python.exe (echo Run install.bat first. & pause & exit /b 1)
.venv\Scripts\python.exe desktop.py
pause
