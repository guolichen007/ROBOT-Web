[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [switch]$ConfirmRestore
)

$ErrorActionPreference = 'Stop'
if (-not $ConfirmRestore) { throw 'Restore overwrites Firebot DEV database content. Pass -ConfirmRestore explicitly.' }

function Assert-NativeCommand {
    param([Parameter(Mandatory = $true)][string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$resolved = (Resolve-Path -LiteralPath $BackupDir).Path
$dump = Join-Path $resolved 'postgres.dump'
if (-not (Test-Path -LiteralPath $dump)) { throw "Backup file does not exist: $dump" }
Set-Location $repo

$expected = ((Get-Content (Join-Path $resolved 'SHA256SUMS.txt') -Raw).Split(' ', [StringSplitOptions]::RemoveEmptyEntries))[0]
$actual = (Get-FileHash $dump -Algorithm SHA256).Hash
if ($expected -ne $actual) { throw 'Database backup SHA-256 verification failed.' }

docker compose -f compose.dev.yml stop api mqtt-ingress command-dispatcher task-worker mock-robot nginx
Assert-NativeCommand 'Stopping Firebot writer services'
docker compose -f compose.dev.yml up -d postgres redis
Assert-NativeCommand 'Starting PostgreSQL and Redis'

$postgresReady = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    docker compose -f compose.dev.yml exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $postgresReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $postgresReady) { throw 'PostgreSQL did not become ready within 60 seconds.' }

docker compose -f compose.dev.yml cp $dump postgres:/tmp/firebot-restore.dump
Assert-NativeCommand 'Copying PostgreSQL backup into the container'
docker compose -f compose.dev.yml exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner /tmp/firebot-restore.dump'
Assert-NativeCommand 'Restoring PostgreSQL backup'

$assets = Join-Path $resolved 'assets'
if (Test-Path -LiteralPath $assets) {
    docker compose -f compose.dev.yml up -d api
    Assert-NativeCommand 'Starting API for asset restore'
    docker compose -f compose.dev.yml cp "$assets/." api:/data/assets
    Assert-NativeCommand 'Restoring application assets'
}
docker compose -f compose.dev.yml exec -T api alembic upgrade head
Assert-NativeCommand 'Validating database migration head'

$postgresUser = (docker compose -f compose.dev.yml exec -T postgres printenv POSTGRES_USER).Trim()
Assert-NativeCommand 'Reading the PostgreSQL user from the container'
$postgresDatabase = (docker compose -f compose.dev.yml exec -T postgres printenv POSTGRES_DB).Trim()
Assert-NativeCommand 'Reading the PostgreSQL database from the container'
$countSql = 'SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM maps),(SELECT count(*) FROM telemetry_samples),(SELECT count(*) FROM fire_events),(SELECT count(*) FROM tasks),(SELECT count(*) FROM audit_logs),(SELECT count(*) FROM commands);'
$counts = docker compose -f compose.dev.yml exec -T postgres psql -v ON_ERROR_STOP=1 -U $postgresUser -d $postgresDatabase -Atc $countSql
Assert-NativeCommand 'Verifying restored business records'
if ([string]::IsNullOrWhiteSpace(($counts -join ''))) { throw 'Restore verification returned no database counts.' }
Write-Host "RESTORE_COUNTS=$counts"

docker compose -f compose.dev.yml up -d --wait
Assert-NativeCommand 'Starting the complete restored Firebot stack'
