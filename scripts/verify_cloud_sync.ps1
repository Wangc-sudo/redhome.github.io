#requires -Version 5.1
<#
.SYNOPSIS
  云上云下一致性核对：本地 git 工作区 vs ECS /opt/dops/repo。
  对全部 git 跟踪文件做 LF 归一化 SHA256 逐文件比对。
  输出 MISSING（云上缺失）/ DIFF（内容不一致）/ COMPARE_DONE 汇总。
  退出码：0 = 完全一致；1 = 存在漂移；2 = 远端仓库路径不存在。

.DESCRIPTION
  现行部署流为「本地 commit -> scp /opt/dops/repo -> compose restart」，
  无自动校验，文档/测试容易漏 scp 造成漂移（2026-09-23 实测 23 文件漂移）。
  改完代码跑一句本脚本即可确认云上云下一致。
  注意：比对基准是本地【工作区】（与 scp 流一致），有未提交改动时会给出警告。

.EXAMPLE
  pwsh scripts/verify_cloud_sync.ps1
  pwsh scripts/verify_cloud_sync.ps1 -Server root@203.205.93.195   # dops-ci
#>
param(
  [string]$Server = 'root@203.205.91.241',
  [string]$RemoteRepo = '/opt/dops/repo'
)
$ErrorActionPreference = 'Stop'
$localRepo = Resolve-Path "$PSScriptRoot/.."

$dirty = git -C $localRepo status --porcelain
if ($dirty) {
  Write-Warning "工作区有 $($dirty.Count) 项未提交改动，比对以工作区内容为准"
}

$tmp = Join-Path $env:TEMP ("dops_verify_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $manifest = Join-Path $tmp 'manifest.txt'
  $compare  = Join-Path $tmp 'compare.sh'

  $files = git -C $localRepo ls-files
  if (-not $files) { throw 'git ls-files 为空，请在仓库内运行' }
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $sb  = [System.Text.StringBuilder]::new()
  foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $localRepo ($f -replace '/', [IO.Path]::DirectorySeparatorChar)))
    if ($bytes -contains 13) { $bytes = [byte[]]($bytes | Where-Object { $_ -ne 13 }) }
    $h = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLower()
    [void]$sb.Append($h).Append('  ').Append($f).Append("`n")
  }
  [System.IO.File]::WriteAllText($manifest, $sb.ToString())

  $bash = @'
#!/bin/bash
cd "$1" || { echo "REMOTE_REPO_NOT_FOUND $1"; exit 2; }
M=0; D=0; OK=0
while read -r h p; do
  if [ ! -f "$p" ]; then
    echo "MISSING $p"; M=$((M+1))
  else
    rh=$(tr -d '\r' < "$p" | sha256sum | cut -d' ' -f1)
    if [ "$rh" != "$h" ]; then
      echo "DIFF $p"; D=$((D+1))
    else
      OK=$((OK+1))
    fi
  fi
done < /tmp/dops_verify_manifest.txt
echo "COMPARE_DONE ok=$OK missing=$M diff=$D"
[ "$M" -eq 0 ] && [ "$D" -eq 0 ]
'@
  [System.IO.File]::WriteAllText($compare, ($bash -replace "`r`n", "`n"))

  scp -o BatchMode=yes $manifest "${Server}:/tmp/dops_verify_manifest.txt"
  if ($LASTEXITCODE -ne 0) { throw "scp manifest 失败（exit $LASTEXITCODE）" }
  scp -o BatchMode=yes $compare "${Server}:/tmp/dops_verify_compare.sh"
  if ($LASTEXITCODE -ne 0) { throw "scp compare 失败（exit $LASTEXITCODE）" }

  ssh -o BatchMode=yes $Server "bash /tmp/dops_verify_compare.sh '$RemoteRepo'"
  $code = $LASTEXITCODE
  if ($code -eq 0) { Write-Output 'SYNC_OK 云上云下完全一致' }
  exit $code
}
finally {
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
