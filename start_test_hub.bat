@echo off
setlocal
title LLM Key Hub TEST - local only
set HUB=F:\llm_hub
set LOGS=%HUB%\logs
if not exist "%LOGS%" mkdir "%LOGS%"
cd /d %HUB%
set HUB_NO_DSH=1
set HUB_BIND_HOST=127.0.0.1
echo === LLM Key Hub TEST MODE ===
echo   pure localhost ^(127.0.0.1 only^) / no public tunnel / no dsh coupling

rem --- dependency: CLIProxyAPI (port 8317) ---
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort 8317 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] CLIProxyAPI already on 8317
) else (
  echo [..] starting CLIProxyAPI
  powershell -NoProfile -Command "Start-Process -FilePath '%HUB%\cliproxy\cli-proxy-api.exe' -ArgumentList '-config','config.yaml' -WorkingDirectory '%HUB%\cliproxy' -WindowStyle Hidden -RedirectStandardOutput '%LOGS%\cliproxy_out.log' -RedirectStandardError '%LOGS%\cliproxy_err.log'"
)

rem --- hub panel (port 8787, copilot auto-starts with hub) ---
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
