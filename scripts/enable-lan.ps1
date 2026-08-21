[CmdletBinding()]
param(
    [string]$LanIP      = '192.168.110.101',
    [string]$Subnet     = '192.168.110.0/24',
    [int]   $PublicPort = 80,
    [int]   $DockerPort = 8080,
    [string]$HostName   = 'firebot.lan',
    [switch]$RestartApi
)

# Enables the Firebot LAN HTTP gateway on a Windows dev machine:
#
#   192.168.110.101:80  ->  127.0.0.1:8080  (Docker nginx)
#
# LAN machines in 192.168.110.0/24 can then open http://firebot.lan
# (no HTTPS, no certificate). Run from an elevated PowerShell:
#
#   .\scripts\enable-lan.ps1
#
# The gateway only ever touches Firebot's own portproxy rule, Firebot's own
# firewall rule, and the "# BEGIN FIREBOT LAN" hosts block. It never kills
# processes and never modifies other portproxy / firewall / hosts entries.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Require-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script requires an elevated (Administrator) PowerShell session.'
    }
}

function Get-FirebotPortProxyRule {
    # True when a v4tov4 portproxy rule for $LanIP:$PublicPort already exists.
    # Matches only the data lines (IP + port numbers), which are locale-independent.
    $show    = netsh interface portproxy show v4tov4 2>$null
    $pattern = '^\s*' + [regex]::Escape($LanIP) + '\s+' + $PublicPort + '\s+'
    return [bool]($show | Select-String -Pattern $pattern)
}

function Get-ManagedHostsLines {
    param([string]$LanIP, [string]$HostName, [string]$Path)
    $markerBegin = '# BEGIN FIREBOT LAN'
    $markerEnd   = '# END FIREBOT LAN'
    $lines = [IO.File]::ReadAllLines($Path)
    $out   = New-Object System.Collections.Generic.List[string]
    $inBlock = $false
    foreach ($line in $lines) {
        if ($line.Trim() -eq $markerBegin) { $inBlock = $true; continue }
        if ($inBlock) {
            if ($line.Trim() -eq $markerEnd) { $inBlock = $false }
            continue
        }
        $out.Add($line)
    }
    $out.Add($markerBegin)
    $out.Add("$LanIP $HostName")
    $out.Add($markerEnd)
    return $out
}

Require-Admin

Write-Host ''
Write-Host 'Firebot LAN Gateway (enable)' -ForegroundColor Cyan
Write-Host "  LanIP=$LanIP  Subnet=$Subnet  PublicPort=$PublicPort  DockerPort=$DockerPort  HostName=$HostName"
Write-Host ''

# A. The LAN IP must actually exist on a local adapter, otherwise the
#    portproxy would silently bind nothing and LAN machines would hang.
$localIp = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanIP -ErrorAction SilentlyContinue
if (-not $localIp) {
    throw "LAN IP $LanIP is not present on any local network adapter. Fix -LanIP and retry."
}
Write-Host "Verified local IP: $LanIP" -ForegroundColor Green

# B. iphlpsvc (IP Helper) owns portproxy listeners and must stay running.
Set-Service -Name iphlpsvc -StartupType Automatic
try { Start-Service -Name iphlpsvc -ErrorAction SilentlyContinue } catch { }
Write-Host 'iphlpsvc: Running + Automatic' -ForegroundColor Green

# Detect whether our own rule already exists so its listener is not mistaken
# for a conflicting process below.
$hasOwnRule = Get-FirebotPortProxyRule

# C. Port 80 guard. A real listener that is not our own portproxy rule is a
#    hard stop; this script never kills processes.
$listeners = @(Get-NetTCPConnection -LocalPort $PublicPort -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $conflicts = @($listeners | Where-Object { -not ($hasOwnRule -and $_.LocalAddress -eq $LanIP) })
    if ($conflicts.Count -gt 0) {
        Write-Host 'LAN_PORT_80_OCCUPIED=YES' -ForegroundColor Red
        foreach ($conn in $conflicts) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            Write-Host "PID=$($conn.OwningProcess)"
            Write-Host "PROCESS=$($proc.ProcessName)"
            Write-Host "ADDRESS=$($conn.LocalAddress):$($conn.LocalPort)"
        }
        Write-Host ''
        Write-Host "Port $PublicPort is in use by another process. Stop it and re-run this script." -ForegroundColor Red
        Write-Host 'This script will not terminate any process.' -ForegroundColor Red
        exit 1
    }
}

# D. Remove only our own previous Firebot port-80 rule (never other rules).
netsh interface portproxy delete v4tov4 listenaddress=$LanIP listenport=$PublicPort 2>&1 | Out-Null

# E. Create the portproxy: LanIP:80 -> 127.0.0.1:8080 (Docker nginx).
netsh interface portproxy add v4tov4 listenaddress=$LanIP listenport=$PublicPort connectaddress=127.0.0.1 connectport=$DockerPort
if ($LASTEXITCODE -ne 0) { throw "portproxy add failed: $LanIP`:$PublicPort -> 127.0.0.1`:$DockerPort" }
Write-Host "portproxy: ${LanIP}:${PublicPort} -> 127.0.0.1:${DockerPort}" -ForegroundColor Green

# F. Windows Firewall: allow inbound TCP 80 on the LAN IP, only from the LAN
#    subnet. RemoteAddress is never "Any".
$fwName = 'Firebot Web LAN HTTP'
if (Get-NetFirewallRule -DisplayName $fwName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $fwName
}
New-NetFirewallRule `
    -DisplayName $fwName `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort $PublicPort `
    -LocalAddress $LanIP `
    -RemoteAddress $Subnet `
    -Action Allow | Out-Null
Write-Host "firewall: $fwName (TCP ${PublicPort}, Local $LanIP, Remote $Subnet, Allow)" -ForegroundColor Green

# G. Managed hosts entry on this dev machine (idempotent block replacement).
$hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$hostsLines = Get-ManagedHostsLines -LanIP $LanIP -HostName $HostName -Path $hostsPath
[IO.File]::WriteAllLines($hostsPath, $hostsLines, [Text.Encoding]::ASCII)
Write-Host "hosts: $LanIP $HostName (managed block)" -ForegroundColor Green

# P0-8. Safely add the LAN origins (hostname + IP) to ALLOWED_ORIGINS in the
#       existing .env, preserving every other value (JWT/refresh/CSRF/admin
#       password untouched) and never printing secrets.
$envPath = Join-Path $repo '.env'
if (Test-Path $envPath) {
    $envLines    = [IO.File]::ReadAllLines($envPath)
    $originNeed  = @("http://$HostName", "http://$LanIP")
    $found       = $false
    for ($i = 0; $i -lt $envLines.Count; $i++) {
        if ($envLines[$i] -match '^\s*ALLOWED_ORIGINS\s*=') {
            $current = $envLines[$i] -replace '^\s*ALLOWED_ORIGINS\s*=\s*', ''
            $origins = New-Object System.Collections.Generic.List[string]
            foreach ($item in ($current.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
                if (-not $origins.Contains($item)) { $origins.Add($item) }
            }
            foreach ($need in $originNeed) {
                if (-not $origins.Contains($need)) { $origins.Add($need) }
            }
            $envLines[$i] = 'ALLOWED_ORIGINS=' + ($origins -join ',')
            $found = $true
            break
        }
    }
    if (-not $found) {
        $envLines += "ALLOWED_ORIGINS=" + ($originNeed -join ',')
    }
    [IO.File]::WriteAllLines($envPath, $envLines, [Text.UTF8Encoding]::new($false))
    Write-Host "env: ALLOWED_ORIGINS now includes $($originNeed -join ' and ') (secrets untouched)" -ForegroundColor Green
} else {
    Write-Host '.env not found; skipping ALLOWED_ORIGINS update.' -ForegroundColor Yellow
}

# P0-9. env_file is only re-read when the container is recreated, so a plain
#       "docker compose restart" would keep the old ALLOWED_ORIGINS.
Write-Host ''
Write-Host 'ALLOWED_ORIGINS changed. Recreate the api container to reload .env:' -ForegroundColor Yellow
Write-Host '  docker compose -f compose.dev.yml up -d --force-recreate api'
if ($RestartApi) {
    Write-Host 'Recreating api as requested...' -ForegroundColor Yellow
    docker compose -f compose.dev.yml up -d --force-recreate api
    if ($LASTEXITCODE -ne 0) { throw 'docker compose recreate api failed.' }
}

Write-Host ''
Write-Host 'Firebot LAN gateway enabled.' -ForegroundColor Green
Write-Host "  LAN (IP):   http://$LanIP"
Write-Host "  LAN (DNS):  http://$HostName"
Write-Host "  Local dev:  http://127.0.0.1:$DockerPort"
