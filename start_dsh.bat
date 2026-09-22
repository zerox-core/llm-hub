@echo off
setlocal
title DeepSeek Harness - cloud hub
echo === DeepSeek Harness launcher ^(dsh => cloud https://hub.zeroxcore.tech^) ===
if not exist "F:\llm_hub\logs" mkdir "F:\llm_hub\logs"
powershell -NoProfile -Command "$found=$false; foreach ($p in (Get-CimInstance Win32_Process -Filter \"Name='node.exe'\")) { if ($p.CommandLine -like '*dsh*bin.js*') { $found=$true } }; if ($found) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [ok] dsh already running
) else (
  echo [..] starting dsh web ^(log => logs\dsh.log^) ...
  powershell -NoProfile -Command "Start-Process -FilePath 'node' -ArgumentList 'F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js','web','--no-open' -WorkingDirectory 'F:\llm_hub' -WindowStyle Hidden -RedirectStandardOutput 'F:\llm_hub\logs\dsh.log' -RedirectStandardError 'F:\llm_hub\logs\dsh_err.log'"
)
powershell -NoProfile -Command "$tok=$null; for($i=0;$i -lt 15;$i++){ if(Test-Path 'F:\llm_hub\logs\dsh.log'){ $m = Select-String -Path 'F:\llm_hub\logs\dsh.log' -Pattern 'token=([A-Za-z0-9_-]+)'; if($m){ $tok=$m[$m.Count-1].Matches[0].Groups[1].Value; break } }; Start-Sleep -Seconds 1 }; if($tok){ [IO.File]::WriteAllText('F:\llm_hub\logs\_dsh_tok.txt', $tok) } else { [IO.File]::WriteAllText('F:\llm_hub\logs\_dsh_tok.txt', '') }"
set /p DSHTOK=<F:\llm_hub\logs\_dsh_tok.txt
del F:\llm_hub\logs\_dsh_tok.txt 2>nul
if "%DSHTOK%"=="" (
  echo [warn] dsh token not found in log, opening panel without it
  start "" https://hub.zeroxcore.tech/harness
) else (
  echo [ok] opening cloud panel with dsh token
  start "" "https://hub.zeroxcore.tech/harness#dsh-token=%DSHTOK%"
)
endlocal
