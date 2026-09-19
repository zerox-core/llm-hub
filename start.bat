@echo off
cd /d %~dp0
rem 已在运行则直接打开页面，避免重复启动
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8787/api/state ^| Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo LLM Key Hub 已在运行，正在打开页面...
  start http://127.0.0.1:8787
  exit /b 0
)
start "" /b cmd /c "timeout /t 2 >nul ^& start http://127.0.0.1:8787"
py -3 server.py
rem 服务退出后停住窗口，方便看错误信息
pause
