[CmdletBinding()]
param([switch]$NoBuild)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function New-RandomValue([int]$Bytes = 36) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($buffer).Replace('+', '-').Replace('/', '_').TrimEnd('=')
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is not installed. Install Docker Desktop first.'
}

try { docker info *> $null } catch {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path $desktop) {
        Start-Process -FilePath $desktop -WindowStyle Hidden
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 2
            try { docker info *> $null; break } catch { }
        }
    }
    try { docker info *> $null } catch { throw 'Start Docker Desktop, wait for the Engine, then retry.' }
}

$created = $false
if (-not (Test-Path '.env')) {
    $created = $true
    $adminPassword = 'Fb!' + (New-RandomValue 18)
    $content = Get-Content '.env.example' -Raw -Encoding UTF8
    $content = $content.Replace('JWT_SECRET=GENERATED_BY_DEV_SCRIPT', "JWT_SECRET=$(New-RandomValue)")
    $content = $content.Replace('REFRESH_SECRET=GENERATED_BY_DEV_SCRIPT', "REFRESH_SECRET=$(New-RandomValue)")
    $content = $content.Replace('CSRF_SECRET=GENERATED_BY_DEV_SCRIPT', "CSRF_SECRET=$(New-RandomValue)")
    $content = $content.Replace('BOOTSTRAP_ADMIN_PASSWORD=GENERATED_BY_DEV_SCRIPT', "BOOTSTRAP_ADMIN_PASSWORD=$adminPassword")
    [IO.File]::WriteAllText((Join-Path $repo '.env'), $content, [Text.UTF8Encoding]::new($false))
}

$composeArgs = @('-f', 'compose.dev.yml', 'up', '-d')
if (-not $NoBuild) { $composeArgs += '--build' }
docker compose @composeArgs
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose startup failed.' }

$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $health = Invoke-RestMethod 'http://localhost:8080/health/ready' -TimeoutSec 3
        if ($health.ok) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { docker compose -f compose.dev.yml ps; throw 'The stack did not become ready in time.' }

Write-Host 'Web:       http://localhost:8080'
Write-Host 'API docs:  http://localhost:8080/api/docs'
Write-Host 'Health:    http://localhost:8080/health/ready'
Write-Host 'Metrics:   http://localhost:8080/metrics'
Write-Host 'Media API: http://localhost:9997/v3/paths/list'
if ($created) {
    Write-Host 'DEV bootstrap (shown once):' -ForegroundColor Yellow
    Write-Host '  username: admin'
    Write-Host "  password: $adminPassword"
}
