@echo off
cd /d %~dp0
rem if hub already running, just open pages
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8787/api/state ^| Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo LLM Key Hub already running, opening pages...
  start "" http://127.0.0.1:8787/harness
  start "" http://127.0.0.1:8787/
  exit /b 0
)
rem hub boots and auto-starts DeepSeek Harness (server-side autostart worker)
start "" /b cmd /c "timeout /t 3 >nul ^& start http://127.0.0.1:8787/harness ^& start http://127.0.0.1:8787/"
py -3 server.py
rem keep window open after server exits to read errors
pause
