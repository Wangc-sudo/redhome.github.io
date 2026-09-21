$ErrorActionPreference = "Continue"
$log = "e:\repos\digital-ops\docker\integration\backfill-watch.log"
Set-Location e:\repos\digital-ops

"$(Get-Date -Format o) watcher v3 started, waiting for wdt-verbose" | Out-File $log
docker wait wdt-verbose | Out-Null
$code = docker inspect wdt-verbose --format '{{.State.ExitCode}}'
"$(Get-Date -Format o) sync container exited, code=$code" | Out-File $log -Append

if ($code -eq "0") {
    "$(Get-Date -Format o) starting extract-mart" | Out-File $log -Append
    docker compose -f docker-compose.integration.yml --profile extract run --rm extract-mart *>&1 | Out-File $log -Append
    "$(Get-Date -Format o) extract-mart finished, exit=$LASTEXITCODE" | Out-File $log -Append
} else {
    "$(Get-Date -Format o) sync FAILED (code=$code), extract-mart NOT started" | Out-File $log -Append
}
"$(Get-Date -Format o) watcher done" | Out-File $log -Append
