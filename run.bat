@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title Voice Transcriber - Local

echo ============================================================
echo   Voice Transcriber  (Groq Whisper)
echo ============================================================
echo.

REM NOTE: This .bat uses ASCII only on purpose. Windows cmd cannot
REM       parse Japanese in batch files, so messages are in English.
REM       The web UI and the .env file are fully Japanese.

REM --- create .env from template on first run ---
if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    echo [SETUP] Created ".env". Notepad will open now.
    echo         Fill in these two lines, then SAVE and CLOSE Notepad:
    echo            GROQ_API_KEY=your_groq_key
    echo            APP_PASSWORD=your_password
    echo.
    notepad ".env"
  )
)

REM --- prepare virtual environment (call venv python directly = move-safe) ---
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [SETUP] Creating Python virtual environment...
  py -m venv .venv 2>nul || python -m venv .venv
)
"%PY%" -c "import sys" 2>nul
if errorlevel 1 (
  echo [SETUP] Rebuilding broken virtual environment...
  rmdir /S /Q .venv 2>nul
  py -m venv .venv 2>nul || python -m venv .venv
)

echo [SETUP] Installing libraries (first time may take a few minutes)...
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q -r requirements.txt

REM ============================================================
REM  ACCESS RANGE (security)
REM    127.0.0.1 = this PC only           (safe, default)
REM    0.0.0.0   = share over the same Wi-Fi/LAN (others can use it)
REM  To share on your LAN, change the next line to: set HOST=0.0.0.0
REM ============================================================
set HOST=127.0.0.1

echo.
echo [START] Opening http://localhost:8000 in your browser...
echo         Access range: %HOST%  (127.0.0.1 = this PC only)
start "" http://localhost:8000
echo         To stop the server, press Ctrl + C in this window.
echo.

"%PY%" -m uvicorn app.main:app --host %HOST% --port 8000

pause
