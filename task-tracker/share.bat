@echo off
setlocal
REM ==== Task Tracker - share the app to phones (double-click this file) ====
cd /d "%~dp0"

echo.
echo   Preparing the phone link. Please wait...
echo.

REM ----- Download the small sharing tool the first time only -----
if not exist "cloudflared.exe" (
    set "CFURL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

    echo   Downloading the sharing tool, method 1 of 2...
    curl -L --fail -o cloudflared.exe "%CFURL%"

    if not exist "cloudflared.exe" (
        echo   Method 1 did not work. Trying method 2 ^(uses company network settings^)...
        powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocol]::Tls12; $p=[System.Net.WebRequest]::GetSystemWebProxy(); $p.Credentials=[System.Net.CredentialCache]::DefaultCredentials; try { Invoke-WebRequest -UseBasicParsing -Proxy $p.GetProxy('https://github.com') -ProxyUseDefaultCredentials -Uri $env:CFURL -OutFile 'cloudflared.exe' } catch { Invoke-WebRequest -UseBasicParsing -Uri $env:CFURL -OutFile 'cloudflared.exe' }"
    )
)

REM ----- Check the tool really downloaded and works -----
if not exist "cloudflared.exe" goto :blocked
cloudflared.exe --version >nul 2>&1
if errorlevel 1 (
    del /q cloudflared.exe >nul 2>&1
    goto :blocked
)

echo.
echo   ====================================================
echo   In a moment a web address will appear below, inside
echo   a box made of  ^|  and  -  characters. It looks like:
echo.
echo        https://words-words-words.trycloudflare.com
echo.
echo   Type that EXACT address into the phone's web browser
echo   and add  /tech  at the end. Keep this window open.
echo   ====================================================
echo.

cloudflared.exe tunnel --url http://127.0.0.1:5000
goto :end

:blocked
echo.
echo   ----------------------------------------------------
echo   COULD NOT GET A PHONE LINK.
echo.
echo   This computer's network is blocking the download or
echo   the connection. This is common on company networks.
echo.
echo   WHAT TO DO:
echo   - Try again on a normal home/phone-hotspot internet
echo     connection, OR
echo   - Ask your IT person to allow "cloudflared" /
echo     github.com downloads, OR
echo   - Use the local-network method (ask IT - see the
echo     bottom of SETUP_GUIDE.txt).
echo   ----------------------------------------------------
echo.

:end
pause
endlocal
