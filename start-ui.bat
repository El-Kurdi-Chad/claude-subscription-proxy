@echo off
rem Opens the Claude Subscription Proxy UI (settings, start/stop, log, requests, quota).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" ui.py
) else if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" ui.py
) else (
    start "" pythonw ui.py
)
