[CmdletBinding()]
param(
    [string]$EnvFile = '.env.server',
    [string]$SecretsDir = 'secrets',
    [string]$BackupPath = 'backups',
    [switch]$AllowOccupiedPorts
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$errors = [Collections.Generic.List[string]]::new()

function Require-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { $errors.Add("Missing $Label`: $Path"); return }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) { $errors.Add("Empty $Label`: $Path") }
}

foreach ($command in 'docker','git') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { $errors.Add("Missing command: $command") }
}
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { $errors.Add('Docker Engine unavailable') }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { $errors.Add('Docker Compose plugin unavailable') }
}
if (-not (Test-Path -LiteralPath $EnvFile)) { $errors.Add("Missing SERVER env: $EnvFile") }
else {
    $envText = Get-Content -LiteralPath $EnvFile -Raw
    if ($envText -match 'REPLACE_|CHANGE_ME|TODO|example\.invalid') { $errors.Add('SERVER env contains placeholder') }
    if ($envText -notmatch '(?m)^ENABLE_API_DOCS=false$') { $errors.Add('SERVER requires ENABLE_API_DOCS=false') }
}

$requiredSecrets = @(
    'app/database_url','app/redis_url','app/jwt_secret','app/refresh_secret','app/csrf_secret',
    'app/bootstrap_admin_password','app/mqtt_platform_password','app/media_publish_token',
    'postgres/password','redis/password','mosquitto/certs/ca.crt','mosquitto/certs/server.crt',
    'mosquitto/certs/server.key','mosquitto/passwords','mosquitto/acl','nginx/server.crt','nginx/server.key'
)
foreach ($relative in $requiredSecrets) { Require-File (Join-Path $SecretsDir $relative) "secret" }
$v1Required = $env:VEHICLE_AGENT_V1_ROLLOUT -eq 'true' -or $env:VEHICLE_AGENT_V1_DOWNLINK_ENABLED -eq 'true' -or $env:COMPOSE_PROFILES -match '(^|,)vehicle-agent(,|$)' -or ($envText -replace '["'' \t\r]', '') -match '(?m)^VEHICLE_AGENT_V1_(ROLLOUT|DOWNLINK_ENABLED)=(true|1|yes|on)$'
$apiId = docker compose --env-file $EnvFile -f docker-compose.server.yml ps -q api 2>$null
if ($LASTEXITCODE -ne 0) { $errors.Add('Cannot determine v1 rollout requirement') }
if ($apiId -and -not $v1Required) {
    $query = @'
from app.core.config import get_settings
from app.db.session import SessionLocal
from sqlalchemy import inspect, text
with SessionLocal() as db:
    columns = {c["name"] for c in inspect(db.get_bind()).get_columns("robots")}
    required = getattr(get_settings(), "vehicle_agent_v1_downlink_enabled", False)
    if "agent_protocol" in columns:
        required = required or bool(db.scalar(text("SELECT id FROM robots WHERE enabled = true AND agent_protocol = :protocol LIMIT 1"), {"protocol": "VEHICLE_AGENT_V1"}))
print("true" if required else "false")
'@
    $dbRequired = docker compose --env-file $EnvFile -f docker-compose.server.yml exec -T api python -c $query 2>$null
    if ($LASTEXITCODE -ne 0 -or $dbRequired -notin @('true','false')) { $errors.Add('Cannot determine DB v1 rollout requirement') }
    if ($dbRequired -eq 'true') { $v1Required = $true }
}
if ($v1Required) {
    python scripts/check_vehicle_agent_acl.py (Join-Path $SecretsDir 'mosquitto/acl')
    if ($LASTEXITCODE -ne 0) { $errors.Add('V1 source ACL gate failed') }
    $brokerId = docker compose --env-file $EnvFile -f docker-compose.server.yml ps -q mosquitto 2>$null
    if ($brokerId) {
        docker compose --env-file $EnvFile -f docker-compose.server.yml exec -T mosquitto sh -c 'grep -Eq "^acl_file[[:space:]]+/mosquitto/secure/acl[[:space:]]*$" /mosquitto/config/mosquitto.conf' *> $null
        if ($LASTEXITCODE -ne 0) { $errors.Add('V1 active broker ACL config differs from expected path') }
        $activeAcl = docker compose --env-file $EnvFile -f docker-compose.server.yml exec -T mosquitto cat /mosquitto/secure/acl 2>$null
        if ($LASTEXITCODE -ne 0) { $errors.Add('V1 active ACL unreadable') }
        $activeAcl | python scripts/check_vehicle_agent_acl.py -
        if ($LASTEXITCODE -ne 0) { $errors.Add('V1 active broker ACL gate failed') }
    }
}
if (-not (Test-Path -LiteralPath $BackupPath)) { New-Item -ItemType Directory -Path $BackupPath | Out-Null }
$probe = Join-Path $BackupPath '.firebot-write-test'
try { [IO.File]::WriteAllText($probe, 'ok'); Remove-Item -LiteralPath $probe -Force } catch { $errors.Add("Backup path is not writable: $BackupPath") }

$cpu = [Environment]::ProcessorCount
$memory = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
$disk = (Get-PSDrive -Name ([IO.Path]::GetPathRoot($repo).Substring(0,1))).Free / 1GB
if ($cpu -lt 2) { $errors.Add("CPU below baseline 2 cores: $cpu") }
if ($memory -lt 4) { $errors.Add("RAM below baseline 4 GiB: $([math]::Round($memory,1))") }
if ($disk -lt 20) { $errors.Add("Disk below baseline 20 GiB free: $([math]::Round($disk,1))") }
if (-not $AllowOccupiedPorts) {
    foreach ($port in 80,443,8883) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { $errors.Add("Port occupied: $port") }
    }
}
try { w32tm /query /status *> $null } catch { $errors.Add('Windows Time status unavailable; verify NTP/domain sync') }

if ((Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $EnvFile)) {
    docker compose --env-file $EnvFile -f docker-compose.server.yml config --quiet
    if ($LASTEXITCODE -ne 0) { $errors.Add('SERVER compose config failed') }
}
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; throw "SERVER_PREFLIGHT=FAIL count=$($errors.Count)" }
Write-Host "SERVER_PREFLIGHT=PASS cpu=$cpu ram_gib=$([math]::Round($memory,1)) disk_free_gib=$([math]::Round($disk,1))"
