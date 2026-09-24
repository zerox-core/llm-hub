# hub_watchdog.ps1 v2 - keep dsh + reverse tunnel + heartbeat alive, dedupe, sync rotated tokens
# scheduled task DshHubWatchdog, every 3 minutes
$ErrorActionPreference = 'SilentlyContinue'
$logDir = 'F:\llm_hub\logs'
$wlog = "$logDir\watchdog.log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
function W([string]$m) { Add-Content $wlog ("{0} {1}" -f (Get-Date -Format s), $m) }

$mutex = New-Object System.Threading.Mutex($false, 'Global\DshHubWatchdog')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {

# --- 1) dsh: exactly one, owning port 3080 ---
$dsh = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like '*dsh*bin.js*' })
if ($dsh.Count -eq 0) {
  W 'dsh not running -> starting via dsh_launch.ps1 (DSH_HOME + pool keys)'
  Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\dsh_launch.ps1'
} elseif ($dsh.Count -gt 1) {
  $owner = @(Get-NetTCPConnection -State Listen -LocalPort 3080 | Select-Object -ExpandProperty OwningProcess -Unique)
  $killed = 0
  foreach ($p in $dsh) {
    if ($owner -notcontains $p.ProcessId) { Stop-Process -Id $p.ProcessId -Force; $killed++ }
  }
  if ($killed -gt 0) { W ("dedupe dsh: killed {0} extra (port owner kept)" -f $killed) }
}

# --- 2) token sync (rotates on every dsh start) ---
$tok = $null
for ($i = 0; $i -lt 25; $i++) {
  if (Test-Path 'F:\llm_hub\logs\dsh.log') {
    $m = Select-String -Path 'F:\llm_hub\logs\dsh.log' -Pattern 'token=([A-Za-z0-9_-]+)'
    if ($m) { $tok = $m[$m.Count - 1].Matches[0].Groups[1].Value; break }
  }
  Start-Sleep -Seconds 1
}
$hbTokChanged = $false
if ($tok) {
  $hbFile = 'F:\llm_hub\logs\_hb_tok.txt'
  $cur = ''
  if (Test-Path $hbFile) { $cur = (Get-Content $hbFile -Raw).Trim() }
  if ($cur -ne $tok) {
    [IO.File]::WriteAllText($hbFile, $tok)
    $hbTokChanged = $true
    W ("hb token synced ({0}...)" -f $tok.Substring(0, [Math]::Min(8, $tok.Length)))
  }
  # R36 fix: dshRemoteToken must be the remote-agent token (data.json remote_console_token),
  # NOT the dsh web session token above (that one made the Android app get 401).
  $rct = $null
  if (Test-Path 'F:\llm_hub\data.json') {
    $m2 = Select-String -Path 'F:\llm_hub\data.json' -Pattern '"remote_console_token"\s*:\s*"([^"]+)"'
    if ($m2) { $rct = $m2.Matches[0].Groups[1].Value }
  }
  if ($rct) {
    foreach ($pf in @('F:\zeroxcore\hermes.properties', 'F:\zeroxcore\.worktrees\mobile-remote-pre\hermes.properties')) {
      if (Test-Path $pf) {
        $txt = [IO.File]::ReadAllText($pf)
        $new = [regex]::Replace($txt, '(?m)^(dshRemoteToken\s*=).*$', ('${1}' + $rct))
        if ($tok) { $new = [regex]::Replace($new, '(?m)^(dshWebToken\s*=).*$', ('${1}' + $tok)) }
        if ($new -ne $txt) { [IO.File]::WriteAllText($pf, $new); W ("props dsh tokens synced (rct {0}...): {1}" -f $rct.Substring(0, [Math]::Min(8, $rct.Length)), $pf) }
      }
    }
  } else {
    W 'WARN: remote_console_token missing in F:\llm_hub\data.json'
  }
} else {
  W 'WARN: no token found in dsh.log (dsh may still be starting)'
}

# --- 3) heartbeat: exactly one ---
$hb = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like '*hub_heartbeat.ps1*' })
if ($hb.Count -gt 1) {
  $keep = ($hb | Sort-Object ProcessId | Select-Object -First 1).ProcessId
  foreach ($p in $hb) { if ($p.ProcessId -ne $keep) { Stop-Process -Id $p.ProcessId -Force } }
  W ("dedupe heartbeat: kept {0}, killed {1}" -f $keep, ($hb.Count - 1))
  $hb = @($hb | Where-Object { $_.ProcessId -eq $keep })
}
if ($hb.Count -eq 1 -and $hbTokChanged) {
  Stop-Process -Id $hb[0].ProcessId -Force
  $hb = @()
  W 'heartbeat killed for token refresh'
}
if ($hb.Count -eq 0) {
  Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\hub_heartbeat.ps1'
  W 'heartbeat started'
}

# --- 4) tunnel: exactly one loop + one ssh ---
$loops = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like '*hub_tunnel.ps1*' })
$ssh = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like '*13080:127.0.0.1:3080*' })
if ($loops.Count -gt 1) {
  # duplicates: full reset (brief tunnel downtime acceptable, rare path)
  foreach ($p in $loops) { Stop-Process -Id $p.ProcessId -Force }
  foreach ($p in $ssh) { Stop-Process -Id $p.ProcessId -Force }
  W ("dedupe tunnel: killed {0} loops + {1} ssh, restarting clean" -f $loops.Count, $ssh.Count)
  Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\hub_tunnel.ps1'
} elseif ($ssh.Count -eq 0) {
  if ($loops.Count -eq 1) { Stop-Process -Id $loops[0].ProcessId -Force }
  Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','F:\llm_hub\hub_tunnel.ps1'
  W 'tunnel restarted (no ssh forwarder)'
} elseif ($ssh.Count -gt 1) {
  # one loop, multiple ssh: kill extras, loop will keep/respawn the survivor path
  $keep = ($ssh | Sort-Object ProcessId | Select-Object -First 1).ProcessId
  foreach ($p in $ssh) { if ($p.ProcessId -ne $keep) { Stop-Process -Id $p.ProcessId -Force } }
  W ("dedupe ssh: kept {0}, killed {1}" -f $keep, ($ssh.Count - 1))
}

} finally {
  $mutex.ReleaseMutex() | Out-Null
}
