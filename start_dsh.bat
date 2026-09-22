@echo off
setlocal
title DeepSeek Harness - cloud hub
echo === DeepSeek Harness launcher ^(dsh =^> cloud https://hub.zeroxcore.tech^) ===
powershell -NoProfile -Command "$found=$false; foreach ($p in (Get-CimInstance Win32_Process -Filter \"Name='node.exe'\")) { if ($p.CommandLine -like '*dsh*bin.js*') { $found=$true } }; if ($found) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] dsh already running
) else (
  echo [..] starting dsh web ...
  powershell -NoProfile -Command "Start-Process -FilePath 'node' -ArgumentList 'F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js','web','--no-open' -WorkingDirectory 'F:\llm_hub' -WindowStyle Hidden"
  timeout /t 4 >nul
)
echo [..] opening cloud panel https://hub.zeroxcore.tech
start "" https://hub.zeroxcore.tech
endlocal
