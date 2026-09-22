# hub-heartbeat-r6: report local dsh liveness to cloud hub every 30s
$ErrorActionPreference = 'SilentlyContinue'
$log = 'F:\llm_hub\logs\heartbeat.log'
$cfg = Get-Content 'F:\llm_hub\data.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$key = $cfg.hub_key
if (-not $key) { Add-Content $log ("{0} START-FAIL: hub_key empty" -f (Get-Date -Format s)); exit 1 }
Add-Content $log ("{0} started" -f (Get-Date -Format s))
$fail = 0
while ($true) {
  $up = [bool](Get-NetTCPConnection -State Listen -LocalPort 3080 -ErrorAction SilentlyContinue)
  if ($up) {
    try {
      $tok = (Get-Content 'F:\llm_hub\logs\_hb_tok.txt' -Raw).Trim()
      Invoke-RestMethod -Uri 'https://hub.zeroxcore.tech/v1/harness/heartbeat' -Method Post -Headers @{Authorization = ('Bearer ' + $key)} -ContentType 'application/json' -Body ('{"token":"' + $tok + '","port":3080}') -TimeoutSec 10 | Out-Null
      $fail = 0
    } catch { $fail++; Add-Content $log ("{0} post-fail {1}: {2}" -f (Get-Date -Format s), $fail, $_.Exception.Message) }
  } else { $fail += 2 }
  if ($fail -ge 6) { Add-Content $log ("{0} giving up after {1} fails" -f (Get-Date -Format s), $fail); break }
  if ($fail -gt 0) { Start-Sleep -Seconds 10 } else { Start-Sleep -Seconds 30 }
}
