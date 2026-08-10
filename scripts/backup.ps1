[CmdletBinding()]
param([string]$OutputRoot = '')

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $repo 'backups' }
Set-Location $repo
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$target = Join-Path $OutputRoot $stamp
New-Item -ItemType Directory -Path $target -Force | Out-Null

docker compose -f compose.dev.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/firebot.dump'
docker compose -f compose.dev.yml cp postgres:/tmp/firebot.dump (Join-Path $target 'postgres.dump')
docker compose -f compose.dev.yml cp api:/data/assets (Join-Path $target 'assets')

$configDir = Join-Path $target 'config'
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
Copy-Item 'infra/mosquitto/*.conf','infra/mosquitto/*.example','infra/mediamtx/*.yml','infra/nginx/*.conf' -Destination $configDir
$manifest = @{
    created_at = (Get-Date).ToUniversalTime().ToString('o')
    profile = 'dev'
    database = 'postgres.dump'
    assets = 'assets'
    rpo_rto = 'TO_BE_CONFIRMED_BY_DEPLOYMENT_OWNER'
} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $target 'manifest.json'), $manifest, [Text.UTF8Encoding]::new($false))
$hash = (Get-FileHash (Join-Path $target 'postgres.dump') -Algorithm SHA256).Hash
[IO.File]::WriteAllText((Join-Path $target 'SHA256SUMS.txt'), "$hash  postgres.dump`n", [Text.UTF8Encoding]::new($false))
Write-Host "BACKUP_DIR=$target"
