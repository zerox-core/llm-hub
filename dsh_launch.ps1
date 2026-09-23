# dsh_launch.ps1 - start dsh with DSH_HOME pinned and hub pool keys exported from data.json
# used by: start_dsh.bat (manual) and hub_watchdog.ps1 (auto-heal). reads keys at launch time -> survives hub key rotation
$ErrorActionPreference = 'SilentlyContinue'
$env:DSH_HOME = 'F:\deepseek-harness\home'
$json = Get-Content 'F:\llm_hub\data.json' -Raw | ConvertFrom-Json
if ($json.hub_key) { $env:HUB_API_KEY = [string]$json.hub_key }
foreach ($p in $json.pools) {
  if ($p.id -and $p.key) {
    $envName = 'HUB_POOL_KEY_' + (([string]$p.id).ToUpper() -replace '[^A-Z0-9]+', '_').Trim('_')
    Set-Item -Path ("env:" + $envName) -Value ([string]$p.key)
  }
}
Start-Process -FilePath 'node' -ArgumentList 'F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js','web','--no-open','--trusted-host','dsh.zeroxcore.tech' -WorkingDirectory 'F:\llm_hub' -WindowStyle Hidden -RedirectStandardOutput 'F:\llm_hub\logs\dsh.log' -RedirectStandardError 'F:\llm_hub\logs\dsh_err.log'
