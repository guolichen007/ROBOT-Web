[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [switch]$ConfirmRestore,
    [string]$ComposeOverride = ''
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
$composeFiles = @('-f', 'compose.dev.yml')
if ($ComposeOverride) {
    $resolvedOverride = (Resolve-Path -LiteralPath $ComposeOverride).Path
    $composeFiles += @('-f', $resolvedOverride)
}

$expected = ((Get-Content (Join-Path $resolved 'SHA256SUMS.txt') -Raw).Split(' ', [StringSplitOptions]::RemoveEmptyEntries))[0]
$actual = (Get-FileHash $dump -Algorithm SHA256).Hash
if ($expected -ne $actual) { throw 'Database backup SHA-256 verification failed.' }

docker compose @composeFiles stop api mqtt-ingress ros-compat-adapter command-dispatcher task-worker mock-robot nginx
Assert-NativeCommand 'Stopping Firebot writer services'
docker compose @composeFiles up -d postgres redis
Assert-NativeCommand 'Starting PostgreSQL and Redis'

$postgresReady = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    docker compose @composeFiles exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $postgresReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $postgresReady) { throw 'PostgreSQL did not become ready within 60 seconds.' }

$postgresUser = (docker compose @composeFiles exec -T postgres printenv POSTGRES_USER).Trim()
Assert-NativeCommand 'Reading the PostgreSQL user from the container'
$postgresDatabase = (docker compose @composeFiles exec -T postgres printenv POSTGRES_DB).Trim()
Assert-NativeCommand 'Reading the PostgreSQL database from the container'
docker compose @composeFiles exec -T postgres sh -c 'dropdb --if-exists --force -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
Assert-NativeCommand 'Recreating an empty PostgreSQL database'

docker compose @composeFiles cp $dump postgres:/tmp/firebot-restore.dump
Assert-NativeCommand 'Copying PostgreSQL backup into the container'
docker compose @composeFiles exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error /tmp/firebot-restore.dump'
Assert-NativeCommand 'Restoring PostgreSQL backup'

$assets = Join-Path $resolved 'assets'
if (Test-Path -LiteralPath $assets) {
    docker compose @composeFiles up -d api
    Assert-NativeCommand 'Starting API for asset restore'
    docker compose @composeFiles cp "$assets/." api:/data/assets
    Assert-NativeCommand 'Restoring application assets'
}
docker compose @composeFiles exec -T api alembic upgrade head
Assert-NativeCommand 'Validating database migration head'

$partitionSql = "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid WHERE p.relname IN ('telemetry_samples','sensor_samples') AND c.relname ~ '_(20[0-9]{2})_([0-9]{2})$';"
$partitionCount = docker compose @composeFiles exec -T postgres psql -v ON_ERROR_STOP=1 -U $postgresUser -d $postgresDatabase -Atc $partitionSql
Assert-NativeCommand 'Verifying restored month partitions'
if ([int](($partitionCount -join '').Trim()) -lt 6) { throw "Expected current and future month partitions, found $partitionCount" }
$defaultSql = "SELECT (SELECT count(*) FROM telemetry_samples_default),(SELECT count(*) FROM sensor_samples_default);"
$defaultRows = docker compose @composeFiles exec -T postgres psql -v ON_ERROR_STOP=1 -U $postgresUser -d $postgresDatabase -Atc $defaultSql
Assert-NativeCommand 'Verifying default partition rows'
Write-Host "RESTORE_PARTITIONS=$partitionCount DEFAULT_ROWS=$defaultRows"

$countSql = 'SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM maps),(SELECT count(*) FROM telemetry_samples),(SELECT count(*) FROM fire_events),(SELECT count(*) FROM tasks),(SELECT count(*) FROM audit_logs),(SELECT count(*) FROM commands);'
$counts = docker compose @composeFiles exec -T postgres psql -v ON_ERROR_STOP=1 -U $postgresUser -d $postgresDatabase -Atc $countSql
Assert-NativeCommand 'Verifying restored business records'
if ([string]::IsNullOrWhiteSpace(($counts -join ''))) { throw 'Restore verification returned no database counts.' }
Write-Host "RESTORE_COUNTS=$counts"

docker compose @composeFiles up -d --wait
Assert-NativeCommand 'Starting the complete restored Firebot stack'
