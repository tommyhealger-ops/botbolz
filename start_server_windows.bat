@echo off
setlocal
cd /d "%~dp0"

if not exist .env (
  echo ERROR: Create .env first. Copy .env.example to .env and fill in its values.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Creating Python environment. This runs only once...
  python -m venv .venv || goto :error
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)

echo Starting the server. Leave this window open.
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
pause
exit /b

:error
echo.
echo Could not start. Install Python 3.11 or newer from https://www.python.org/downloads/
pause
exit /b 1
