@echo off
title KTS - Backup Database
cd /d "%~dp0"

echo ============================================
echo   KTS INSTITUTE MANAGER - Backup
echo ============================================
echo.

set PYEXE=C:\Users\Tabrez\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe
if not exist "%PYEXE%" set PYEXE=python

set SQLITE_PATH=%~dp0kts_institute.db

"%PYEXE%" backup_db.py --dir "%~dp0backups" --keep 30

echo.
echo Your backups are in the "backups" folder.
echo Copy that folder to a USB drive or cloud storage regularly.
echo.
pause
