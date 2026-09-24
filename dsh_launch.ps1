# dsh_launch.ps1 - start dsh with DSH_HOME pinned and hub pool keys exported from data.json
# used by: start_dsh.bat (manual) and hub_watchdog.ps1 (auto-heal). reads keys at launch time -> survives hub key rotation
$ErrorActionPreference = 'SilentlyContinue'
$env:DSH_HOME = 'F:\deepseek-harness\home'
# R27 2026-09-24: -Encoding UTF8 is REQUIRED. data.json contains raw UTF-8 Chinese (pool names);
# without it Get-Content decodes as ANSI/GBK and the multibyte mismatch can swallow quote chars,
# silently failing ConvertFrom-Json (SilentlyContinue) -> dsh started with NO pool keys ->
# every model call died with MISSING_CREDENTIAL. Failures now leave a trace in the log.
$launchLog = 'F:\llm_hub\logs\dsh_launch_env.log'
$json = Get-Content 'F:\llm_hub\data.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $json) {
  Add-Content $launchLog ('{0} FATAL data.json parse FAILED - no pool keys injected' -f (Get-Date -Format s))
} else {
  Add-Content $launchLog ('{0} data.json parsed OK (R27 UTF8 fix)' -f (Get-Date -Format s))
}
if ($json.hub_key) { $env:HUB_API_KEY = [string]$json.hub_key }
foreach ($p in $json.pools) {
  if ($p.id -and $p.key) {
    $envName = 'HUB_POOL_KEY_' + (([string]$p.id).ToUpper() -replace '[^A-Z0-9]+', '_').Trim('_')
    Set-Item -Path ("env:" + $envName) -Value ([string]$p.key)
  }
}
$poolEnvNames = (Get-ChildItem env:HUB_POOL_KEY_* -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) -join ','
Add-Content $launchLog ('{0} pool env set: {1}' -f (Get-Date -Format s), $poolEnvNames)
if (-not $poolEnvNames) { Add-Content $launchLog ('{0} WARN no HUB_POOL_KEY_* env present at launch' -f (Get-Date -Format s)) }

Start-Process -FilePath 'node' -ArgumentList 'F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js','web','--no-open','--trusted-host','dsh.zeroxcore.tech' -WorkingDirectory 'F:\llm_hub' -WindowStyle Hidden -RedirectStandardOutput 'F:\llm_hub\logs\dsh.log' -RedirectStandardError 'F:\llm_hub\logs\dsh_err.log'
