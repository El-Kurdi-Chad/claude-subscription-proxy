@echo off
title Claude Subscription Proxy

cd /d "%~dp0"

echo ========================================
echo   Claude Subscription Proxy
echo ========================================
echo.

call .venv\Scripts\activate.bat

echo Starte Proxy...
echo.
python proxy.py

pause