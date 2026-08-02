@echo off
title KTS Institute Manager
cd /d "%~dp0"

echo ============================================
echo   KTS INSTITUTE MANAGER - Starting up...
echo ============================================
echo.

set PYEXE=C:\Users\Tabrez\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe
if not exist "%PYEXE%" set PYEXE=python

REM Persistent local database, stored next to this file.
set FLASK_ENV=development
set SQLITE_PATH=%~dp0kts_institute.db

REM Fixed key so your login session survives restarts.
set SECRET_KEY=local-kts-key-change-if-you-ever-go-online-2026
set ADMIN_PASSWORD=admin123
set HOST=127.0.0.1
set PORT=5000

echo Your data file:
echo   %SQLITE_PATH%
echo.
echo Login:  admin  /  admin123
echo.
echo Opening your browser in a moment...
echo KEEP THIS BLACK WINDOW OPEN while you use the app.
echo Close it when you are finished.
echo.

start "" cmd /c "timeout /t 3 >nul && start http://127.0.0.1:5000"

"%PYEXE%" app.py

echo.
echo The app has stopped.
pause
