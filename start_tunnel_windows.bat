@echo off
setlocal
title PC Control - Internet connection

where cloudflared >nul 2>nul
if not errorlevel 1 goto :start

echo cloudflared is not installed yet.
echo It is needed so Telegram can open the panel from the Internet.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :manual

echo Installing cloudflared automatically. Please wait...
winget install --id Cloudflare.cloudflared -e --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :manual
echo.
echo Installation finished. Close this window, open start_tunnel_windows.bat again,
echo then allow Windows to open it if it asks.
pause
exit /b

:start
echo.
echo A separate PowerShell window will now open and will stay open.
echo Do not close it while you use the Telegram panel.
echo Copy the https://...trycloudflare.com address shown there.
echo The same text is saved in tunnel.log in this project folder.
echo.
set "TUNNEL_LOG=%~dp0tunnel.log"
powershell -NoLogo -NoExit -Command "& cloudflared tunnel --url http://localhost:8000 2>&1 | Tee-Object -FilePath '%TUNNEL_LOG%'"
pause
exit /b

:manual
echo.
echo Automatic installation did not work.
echo Open this page in a browser and install cloudflared for Windows:
echo https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
echo After installing, close PowerShell, open it again, and double-click this file.
pause
exit /b 1
