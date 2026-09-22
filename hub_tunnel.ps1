# hub-tunnel-r7: keep reverse tunnel alive so dsh.zeroxcore.tech => local dsh
$ErrorActionPreference = 'SilentlyContinue'
$log = 'F:\llm_hub\logs\tunnel.log'
Add-Content $log ("{0} tunnel loop start" -f (Get-Date -Format s))
while ($true) {
  & ssh -N -R 13080:127.0.0.1:3080 zeroxcore -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new 2>> $log
  Add-Content $log ("{0} ssh exit {1}, retry in 10s" -f (Get-Date -Format s), $LASTEXITCODE)
  Start-Sleep -Seconds 10
}
