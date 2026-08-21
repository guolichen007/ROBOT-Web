[CmdletBinding()]
param(
    [string]$ServerIP = '192.168.110.101',
    [string]$HostName = 'firebot.lan',
    [int]   $Port     = 80
)

# FALLBACK setup for a LAN demo machine in the same 192.168.110.0/24 subnet.
#
# The primary deployment mode is router DNS: add "firebot.lan A 192.168.110.101"
# on 192.168.110.1 (see docs/LAN_WEB_ACCESS.md), then clients need nothing at all.
# Use this script ONLY when the router DNS is not yet configured and a machine
# needs temporary access right now — it writes a hosts entry as a stopgap.
#
# Run once from an elevated PowerShell:
#
#   .\install-firebot-lan-client.ps1

$ErrorActionPreference = 'Stop'

function Require-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script requires an elevated (Administrator) PowerShell session.'
    }
}

Require-Admin

Write-Host ''
Write-Host 'Firebot LAN client setup' -ForegroundColor Cyan
Write-Host "  ServerIP=$ServerIP  HostName=$HostName  Port=$Port"
Write-Host ''

# 2. Verify the dev machine is reachable on the gateway port before touching anything.
$reachable = Test-NetConnection -ComputerName $ServerIP -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $reachable) {
    throw "Cannot reach ${ServerIP}:${Port}. Confirm the dev machine is on this LAN and the gateway is enabled (scripts\enable-lan.ps1 on the dev machine)."
}
Write-Host "Reachable: ${ServerIP}:${Port}" -ForegroundColor Green

# 3. Idempotent hosts block. Only the "# BEGIN FIREBOT LAN" block is touched.
$hostsPath   = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$markerBegin = '# BEGIN FIREBOT LAN'
$markerEnd   = '# END FIREBOT LAN'
$lines = [IO.File]::ReadAllLines($hostsPath)
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
$out.Add("$ServerIP $HostName")
$out.Add($markerEnd)
[IO.File]::WriteAllLines($hostsPath, $out, [Text.Encoding]::ASCII)
Write-Host "hosts: $ServerIP $HostName (managed block)" -ForegroundColor Green

# 4. Flush the DNS cache so the new hosts entry is picked up immediately.
ipconfig /flushdns | Out-Null

# 5. Verify name resolution to the expected IP (hosts file is consulted first).
$resolved = $false
try {
    $dns = Resolve-DnsName -Name $HostName -ErrorAction Stop
    $resolved = [bool]($dns | Where-Object { $_.IPAddress -eq $ServerIP })
} catch {
    $resolved = $false
}
if (-not $resolved) {
    try {
        $addrs = [Net.Dns]::GetHostAddresses($HostName)
        $resolved = [bool]($addrs | Where-Object { $_.IPAddressToString -eq $ServerIP })
    } catch {
        $resolved = $false
    }
}
if ($resolved) {
    Write-Host "resolved: $HostName -> $ServerIP" -ForegroundColor Green
} else {
    Write-Host "WARNING: could not confirm $HostName resolves to $ServerIP." -ForegroundColor Yellow
}

# 6. Verify the web endpoint returns HTTP 200 with ok=true.
$health = Invoke-WebRequest -Uri "http://${HostName}/health/ready" -TimeoutSec 10 -UseBasicParsing -ErrorAction SilentlyContinue
if ($health -and $health.StatusCode -eq 200 -and $health.Content -match '"ok"\s*:\s*true') {
    Write-Host ''
    Write-Host 'FIREBOT_LAN_CLIENT_READY=YES' -ForegroundColor Green
    Write-Host "Web: http://$HostName"
    exit 0
}

Write-Host ''
Write-Host 'FIREBOT_LAN_CLIENT_READY=NO' -ForegroundColor Red
Write-Host 'The health endpoint did not answer as expected. Check the gateway on the dev machine.'
exit 1
