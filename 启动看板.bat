@echo off
chcp 65001 >nul
title Olist Ops Dashboard
cd /d "C:\Users\28223\Desktop\olist-ops-monitoring"

echo ============================================================
echo   Olist Ops Dashboard  /  Launcher
echo ------------------------------------------------------------
echo   Project : C:\Users\28223\Desktop\olist-ops-monitoring
echo   URL     : http://localhost:8501
echo   Stop    : press Ctrl+C in this window
echo ============================================================
echo.

echo [1/2] Checking PostgreSQL service ...
sc query postgresql-x64-18 | findstr /C:"RUNNING" >nul
if errorlevel 1 (
    echo   WARN: PostgreSQL is not running. Trying to start ...
    net start postgresql-x64-18
) else (
    echo   OK: PostgreSQL is running.
)
echo.

echo [2/2] Starting Streamlit ...
echo       First load may take 5-10 seconds.
echo.
"C:\Users\28223\AppData\Local\Microsoft\WindowsApps\python3.11.exe" -m streamlit run app.py

echo.
echo ============================================================
echo   Server stopped.
echo ============================================================
pause
