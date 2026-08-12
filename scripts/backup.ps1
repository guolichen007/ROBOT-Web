[CmdletBinding()]
param(
    [string]$OutputRoot = '',
    [string]$ComposeOverride = ''
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $repo 'backups' }
Set-Location $repo
$composeFiles = @('-f', 'compose.dev.yml')
if ($ComposeOverride) {
    $resolvedOverride = (Resolve-Path -LiteralPath $ComposeOverride).Path
    $composeFiles += @('-f', $resolvedOverride)
}
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$target = Join-Path $OutputRoot $stamp
New-Item -ItemType Directory -Path $target -Force | Out-Null

docker compose @composeFiles exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/firebot.dump'
docker compose @composeFiles cp postgres:/tmp/firebot.dump (Join-Path $target 'postgres.dump')
docker compose @composeFiles cp api:/data/assets (Join-Path $target 'assets')

$configDir = Join-Path $target 'config'
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
Copy-Item 'infra/mosquitto','infra/mediamtx','infra/nginx' -Destination $configDir -Recurse
$migration = (docker compose @composeFiles exec -T postgres psql -U firebot -d firebot -Atc 'SELECT version_num FROM alembic_version;').Trim()
$partitionSql = "SELECT coalesce(json_agg(c.relname ORDER BY c.relname),'[]'::json) FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid WHERE p.relname IN ('telemetry_samples','sensor_samples');"
$partitions = docker compose @composeFiles exec -T postgres psql -U firebot -d firebot -Atc $partitionSql
$partitionList = [string[]]($partitions | ConvertFrom-Json)
$defaultSql = "SELECT json_build_object('telemetry_samples_default',(SELECT count(*) FROM telemetry_samples_default),'sensor_samples_default',(SELECT count(*) FROM sensor_samples_default));"
$defaultRows = docker compose @composeFiles exec -T postgres psql -U firebot -d firebot -Atc $defaultSql
$manifest = @{
    created_at = (Get-Date).ToUniversalTime().ToString('o')
    profile = 'dev'
    database = 'postgres.dump'
    assets = 'assets'
    migration_revision = $migration
    partitions = $partitionList
    default_partition_rows = ($defaultRows | ConvertFrom-Json)
    rpo_rto = 'TO_BE_CONFIRMED_BY_DEPLOYMENT_OWNER'
} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $target 'manifest.json'), $manifest, [Text.UTF8Encoding]::new($false))
$hash = (Get-FileHash (Join-Path $target 'postgres.dump') -Algorithm SHA256).Hash
[IO.File]::WriteAllText((Join-Path $target 'SHA256SUMS.txt'), "$hash  postgres.dump`n", [Text.UTF8Encoding]::new($false))
Write-Host "BACKUP_DIR=$target"
