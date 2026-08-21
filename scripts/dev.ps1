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
        $healthJson = & curl.exe --noproxy '*' --fail --silent --show-error --max-time 3 'http://127.0.0.1:8080/health/ready'
        if ($LASTEXITCODE -ne 0) { throw 'health request failed' }
        $health = $healthJson | ConvertFrom-Json
        if ($health.ok) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { docker compose -f compose.dev.yml ps; throw 'The stack did not become ready in time.' }

Write-Host ''
Write-Host 'Firebot DEV READY' -ForegroundColor Green
Write-Host ''
Write-Host '本机开发:' -ForegroundColor Cyan
Write-Host '  Web:       http://127.0.0.1:8080'
Write-Host '  API docs:  http://127.0.0.1:8080/api/docs'
Write-Host '  Health:    http://127.0.0.1:8080/health/ready'
Write-Host '  Metrics:   http://127.0.0.1:8080/metrics'
Write-Host '  Media API: http://localhost:9997/v3/paths/list'
Write-Host ''
Write-Host '局域网:' -ForegroundColor Cyan
$lanReady = $false
try {
    & curl.exe --noproxy '*' --fail --silent --show-error --max-time 2 'http://firebot.lan/health/ready' *> $null
    if ($LASTEXITCODE -eq 0) { $lanReady = $true }
} catch {
    $lanReady = $false
}
if ($lanReady) {
    Write-Host '  http://firebot.lan'
} else {
    Write-Host '  NOT CONFIGURED'
    Write-Host ''
    Write-Host '  如需局域网展示，请在管理员 PowerShell 运行:' -ForegroundColor Yellow
    Write-Host '    .\scripts\enable-lan.ps1'
}
if ($created) {
    Write-Host 'DEV bootstrap (shown once):' -ForegroundColor Yellow
    Write-Host '  username: admin'
    Write-Host "  password: $adminPassword"
}
