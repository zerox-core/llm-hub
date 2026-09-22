@echo off
setlocal
title LLM Key Hub TEST - local only
cd /d F:\llm_hub
set HUB_NO_DSH=1
set HUB_BIND_HOST=127.0.0.1
echo === LLM Key Hub TEST MODE ===
echo   pure localhost ^(127.0.0.1 only^) / no public tunnel / no dsh coupling
echo   for pool testing and dev; merge to master + redeploy cloud after verified
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8787/api/state ^| Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo [ok] test hub already running, opening panel...
  start "" http://127.0.0.1:8787/
  exit /b 0
)
start "" /b cmd /c "timeout /t 3 >nul ^& start http://127.0.0.1:8787/"
py -3 server.py
pause
endlocal
