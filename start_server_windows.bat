@echo off
setlocal
cd /d "%~dp0"

if not exist .env (
  echo ERROR: Create .env first. Copy .env.example to .env and fill in its values.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 goto :no_python

if not exist .venv\Scripts\python.exe (
  echo Creating Python environment. This runs only once...
  python -m venv .venv || goto :error
)

echo Checking program components. This can take a few minutes on first run...
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
echo.
echo Starting the server. Leave this window open.
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
echo.
echo The server stopped. Read the error above. The most common cause is an unfinished .env file.
pause
exit /b

:no_python
echo.
echo Python was not found. Install Python 3.11 or newer from:
echo https://www.python.org/downloads/
echo During installation tick: Add Python to PATH
pause
exit /b 1

:error
echo.
echo Could not prepare or start the server. Check your internet connection and .env file.
pause
exit /b 1
