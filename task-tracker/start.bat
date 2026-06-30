@echo off
REM ==== Task Tracker - start the app (double-click this file) ====
cd /d "%~dp0"

echo.
echo   Starting the Task Tracker...
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   PROBLEM: setup not done yet. See STEP 1 in SETUP_GUIDE.txt.
    echo.
    pause
    exit /b
)

echo   ====================================================
echo   The Task Tracker is now RUNNING.
echo.
echo   Leave THIS window open while in use.
echo   To STOP: close this window.
echo   ====================================================
echo.

".venv\Scripts\python.exe" runserver.py

echo.
echo   The server has stopped.
pause
