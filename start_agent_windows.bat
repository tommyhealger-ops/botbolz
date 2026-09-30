@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Creating Python environment. This runs only once...
  python -m venv .venv || goto :error
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)

if not exist agent-state.json (
  echo.
  echo Enter the address and one-time code created on the server PC.
  set /p "SERVER_URL=Server address (example https://orange-dog.trycloudflare.com):"
  set /p "PAIRING_CODE=One-time pairing code:"
  if "%SERVER_URL%"=="" goto :error
  if "%PAIRING_CODE%"=="" goto :error
  .venv\Scripts\python.exe agent.py pair --server "%SERVER_URL%" --code "%PAIRING_CODE%" || goto :error
)

echo Agent is running. Leave this window open.
.venv\Scripts\python.exe agent.py run
pause
exit /b

:error
echo.
echo Could not start. Check the server address, the one-time code, and Python installation.
pause
exit /b 1
