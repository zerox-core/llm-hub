@echo off
setlocal
set HUB=F:\llm_hub
set LOGS=%HUB%\logs
set YML=C:\Users\Lenovo\.cloudflared\llm-hub.yml
if not exist "%LOGS%" mkdir "%LOGS%"

echo === LLM Hub launcher ===

rem --- 1. CLIProxyAPI (port 8317) ---
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8317/v1/models'; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo [ok] CLIProxyAPI already on 8317
) else (
  echo [..] starting CLIProxyAPI
  powershell -NoProfile -Command "Start-Process -FilePath '%HUB%\cliproxy\cli-proxy-api.exe' -ArgumentList '-config','config.yaml' -WorkingDirectory '%HUB%\cliproxy' -WindowStyle Hidden -RedirectStandardOutput '%LOGS%\cliproxy_out.log' -RedirectStandardError '%LOGS%\cliproxy_err.log'"
)

rem --- 2. Hub panel (port 8787) ---
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8787/api/state'; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo [ok] Hub already on 8787
) else (
  echo [..] starting Hub
  powershell -NoProfile -Command "Start-Process -FilePath 'py' -ArgumentList '-3','server.py' -WorkingDirectory '%HUB%' -WindowStyle Hidden -RedirectStandardOutput '%LOGS%\hub_out.log' -RedirectStandardError '%LOGS%\hub_err.log'"
)

rem --- 3. public tunnel ag.zxc66.asia ---
powershell -NoProfile -Command "$found=$false; foreach ($p in (Get-CimInstance Win32_Process -Filter 'Name=''cloudflared.exe''')) { if ($p.CommandLine -like '*16ec6d44*') { $found=$true } }; if ($found) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] public tunnel already running
) else (
  echo [..] starting public tunnel
  powershell -NoProfile -Command "Start-Process -FilePath 'cloudflared' -ArgumentList 'tunnel','--config','%YML%','run','16ec6d44-d9e7-47a1-b384-32bc38800aff' -WindowStyle Hidden -RedirectStandardOutput '%LOGS%\tunnel_out.log' -RedirectStandardError '%LOGS%\tunnel_err.log'"
)

rem --- 4. wait for hub ---
powershell -NoProfile -Command "$ok=$false; foreach ($i in 1..25) { try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8787/api/state'; $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if ($ok) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] Hub up
) else (
  echo [FAIL] Hub not up - see %LOGS%\hub_err.log
  pause
  exit /b 1
)

rem --- 5. start DeepSeek Harness (idempotent; hub also autostarts it on boot) ---
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -TimeoutSec 8 'http://127.0.0.1:8787/api/harness/start' ^| Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo [ok] DeepSeek Harness start requested
) else (
  echo [warn] Harness start request failed - open Harness page to retry
)

rem --- 6. open pages: DeepSeek Harness + channel manager ---
start "" http://127.0.0.1:8787/harness
start "" http://127.0.0.1:8787/
endlocal
exit /b 0
