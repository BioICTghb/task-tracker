@echo off
REM ==== Task Tracker - start the app (double-click this file) ====
cd /d "%~dp0"

echo.
echo   Starting the Task Tracker. Please wait...
echo   (The first time can take a few minutes. This is normal.)
echo.

REM Create the private environment the first time only
if not exist ".venv" (
    echo   Setting up for the first time...
    python -m venv .venv
    if errorlevel 1 goto :no_python
)

call ".venv\Scripts\activate.bat"

REM Install the parts the app needs (skips quickly if already done)
python -m pip install --quiet --upgrade pip
pip install --quiet Flask python-dotenv waitress
if errorlevel 1 goto :no_python

REM Create a simple settings file the first time only
if not exist ".env" (
    >.env  echo DB_BACKEND=sqlite
    >>.env echo SECRET_KEY=fieldtest
)

echo.
echo   ====================================================
echo   The Task Tracker is now RUNNING.
echo.
echo   Leave THIS window open while testing.
echo   To put it on phones, also double-click  share.bat
echo.
echo   To STOP: close this window.
echo   ====================================================
echo.

waitress-serve --listen=127.0.0.1:5000 app:app
goto :end

:no_python
echo.
echo   ----------------------------------------------------
echo   PROBLEM: Python is not installed yet.
echo   Please follow STEP 1 in SETUP_GUIDE.txt first,
echo   then double-click this file again.
echo   ----------------------------------------------------
echo.

:end
pause
