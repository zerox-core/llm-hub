# dsh_update.ps1 - dsh 升级器（R43, 2026-09-25）
# 铁律（用户 2026-09-25 拍板）：任何 dsh 更新前必须先备份，备份是每次更新前的必要步骤。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File F:\llm_hub\dsh_update.ps1 [-Tag latest|next|0.1.7-rc.2]
# 流程：备份 app 全量 + home 关键件 -> 停 dsh -> npm install -> watchdog 3 分钟内自愈拉起。
# 回滚：robocopy <备份目录> F:\deepseek-harness\app /MIR，然后等 watchdog 自愈即可。
param([string]$Tag = "latest")
$ErrorActionPreference = 'Stop'
$APP = 'F:\deepseek-harness\app'
$DSHHOME = 'F:\deepseek-harness\home'
$BAK = 'F:\deepseek-harness\backups'
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'

# --- 1) 备份（必要步骤，失败即中止，绝不未备份先升级） ---
$pkg = Get-Content "$APP\node_modules\@deepseek-ai\dsh\package.json" -Raw | ConvertFrom-Json
$cur = $pkg.version
$dst = "$BAK\app-$cur-$ts"
Write-Host "[1/4] backup $APP -> $dst"
New-Item -ItemType Directory -Force $dst | Out-Null
robocopy $APP $dst /E /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -gt 7) { throw "robocopy app failed rc=$LASTEXITCODE - abort, nothing changed" }
# home 关键小件：settings.yaml（扫描器渲染）、插件注册、插件部署副本
$hdir = "$dst\home-critical"
New-Item -ItemType Directory -Force "$hdir\profiles\web" | Out-Null
Copy-Item "$DSHHOME\settings.yaml" $hdir -ErrorAction SilentlyContinue
Copy-Item "$DSHHOME\profiles\web\cordis.patch.yml" "$hdir\profiles\web" -ErrorAction SilentlyContinue
if (Test-Path "$DSHHOME\profiles\web\node_modules\@dsh-local") {
  robocopy "$DSHHOME\profiles\web\node_modules\@dsh-local" "$hdir\profiles\web\node_modules\@dsh-local" /E /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
}
# 滚动保留最近 3 份
New-Item -ItemType Directory -Force $BAK | Out-Null
Get-ChildItem $BAK -Directory | Sort-Object Name -Descending | Select-Object -Skip 3 | Remove-Item -Recurse -Force

# --- 2) 停 dsh（防 node_modules 文件锁；watchdog 会在 3 分钟内自愈拉起） ---
Write-Host "[2/4] stop dsh"
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like '*dsh*bin.js*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# --- 3) 升级 ---
Write-Host "[3/4] npm install @deepseek-ai/dsh@$Tag"
npm --prefix $APP install "@deepseek-ai/dsh@$Tag"
if ($LASTEXITCODE -ne 0) { throw "npm install failed rc=$LASTEXITCODE - dsh 已停，回滚见脚本头注释" }

# --- 4) 结果 ---
$pkg2 = Get-Content "$APP\node_modules\@deepseek-ai\dsh\package.json" -Raw | ConvertFrom-Json
Write-Host "[4/4] done: $cur -> $($pkg2.version)。watchdog 3 分钟内自动拉起 dsh；扫描器随后自动重渲染 settings.yaml。"
Write-Host "      升级后请回归：手机远程控制台（remote-agent 插件）+ 模型列表。"
