@echo off
setlocal
title DeepSeek Harness - cloud hub
echo === DeepSeek Harness launcher ^(dsh => cloud https://hub.zeroxcore.tech^) ===
if not exist "F:\llm_hub\logs" mkdir "F:\llm_hub\logs"
rem R8: pin dsh home to migrated location (grouped settings.yaml lives here)
set "DSH_HOME=F:\deepseek-harness\home"

powershell -NoProfile -Command "$found=$false; foreach ($p in (Get-CimInstance Win32_Process -Filter \"Name='node.exe'\")) { if ($p.CommandLine -like '*dsh*bin.js*') { $found=$true } }; if ($found) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] dsh already running
) else (
  echo [..] starting dsh web ^(log => logs\dsh.log^) ...
  powershell -NoProfile -Command "Start-Process -FilePath 'node' -ArgumentList 'F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js','web','--no-open','--trusted-host','dsh.zeroxcore.tech' -WorkingDirectory 'F:\llm_hub' -WindowStyle Hidden -RedirectStandardOutput 'F:\llm_hub\logs\dsh.log' -RedirectStandardError 'F:\llm_hub\logs\dsh_err.log'"
)

powershell -NoProfile -Command "$tok=$null; for($i=0;$i -lt 20;$i++){ if(Test-Path 'F:\llm_hub\logs\dsh.log'){ $m = Select-String -Path 'F:\llm_hub\logs\dsh.log' -Pattern 'token=([A-Za-z0-9_-]+)'; if($m){ $tok=$m[$m.Count-1].Matches[0].Groups[1].Value; break } }; Start-Sleep -Seconds 1 }; if($tok){ [IO.File]::WriteAllText('F:\llm_hub\logs\_dsh_tok.txt', $tok) } else { [IO.File]::WriteAllText('F:\llm_hub\logs\_dsh_tok.txt', '') }"
set /p DSHTOK=<F:\llm_hub\logs\_dsh_tok.txt
del F:\llm_hub\logs\_dsh_tok.txt 2>nul
if "%DSHTOK%"=="" (
  echo [FAIL] dsh token not found in logs\dsh.log - dsh may not have started, check F:\llm_hub\logs\dsh_err.log
  pause
  exit /b 1
)

echo %DSHTOK%>F:\llm_hub\logs\_hb_tok.txt
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object {$_.CommandLine -like '*hub_heartbeat.ps1*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\hub_heartbeat.ps1'"
echo [ok] cloud heartbeat started ^(every 30s =^> hub.zeroxcore.tech^)

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | Where-Object {$_.CommandLine -like '*13080:127.0.0.1:3080*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\hub_tunnel.ps1'"
echo [ok] reverse tunnel started ^(dsh.zeroxcore.tech =^> local dsh 3080^)

echo [ok] opening local dsh ^(authenticated^)
start "" "http://127.0.0.1:3080/?token=%DSHTOK%"

echo [ok] opening cloud panel
start "" "https://hub.zeroxcore.tech/harness#dsh-token=%DSHTOK%"

echo === launcher done, this window closes in 5 seconds ===
timeout /t 5 >nul
endlocal
