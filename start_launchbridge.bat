@echo off
title Launch Bridge LLC - Startup
echo ============================================
echo  Launch Bridge LLC - Automated Filing Startup
echo ============================================
echo.

set CHROME_PATH="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME_PATH% set CHROME_PATH="%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

echo [1/3] Starting Chrome with remote debugging on port 9222...
start "" %CHROME_PATH% --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug-new" "https://cis.scc.virginia.gov/Account/Login"

echo [2/3] Waiting 3 seconds for Chrome to open...
timeout /t 3 /nobreak >nul

echo       Chrome should now be open at the SCC login page. If this is a
echo       brand-new C:\chrome-debug-new profile, log in once manually -
echo       Chrome remembers it next time via that same folder, so this
echo       is normally a one-time step, not something to do every launch.
echo.

echo [3/3] Starting the filing poller and SCC status checker in WSL...
start "Launch Bridge - Filing Poller" wsl.exe -d Ubuntu bash -lc "cd ~/ai-business-launcher && source .venv/bin/activate && python3 -m app.local_filing_poller"
start "Launch Bridge - SCC Status Checker" wsl.exe -d Ubuntu bash -lc "cd ~/ai-business-launcher && source .venv/bin/activate && python3 -m app.check_scc_status"

echo.
echo Both are now running in their own windows - leave them open.
echo Leave this machine on and Chrome open for automatic filing to keep working.
echo You can close this window.
pause
