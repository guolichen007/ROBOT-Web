[CmdletBinding()]
param(
    [string]$LanIP      = '192.168.110.101',
    [int]   $PublicPort = 80
)

# Disables the Firebot LAN HTTP gateway. Removes ONLY Firebot's own artifacts:
#
#   1. the portproxy rule  192.168.110.101:80 -> 127.0.0.1:8080
#   2. the firewall rule   "Firebot Web LAN HTTP"
#   3. the hosts managed   "# BEGIN FIREBOT LAN" ... "# END FIREBOT LAN" block
#
# It never clears all portproxy rules, never deletes other firewall rules, and
# never modifies other hosts lines. Run from an elevated PowerShell:
#
#   .\scripts\disable-lan.ps1

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
Write-Host 'Firebot LAN Gateway (disable)' -ForegroundColor Cyan
Write-Host ''

# 1. Remove only our own port-80 portproxy rule. "Element not found" is fine.
netsh interface portproxy delete v4tov4 listenaddress=$LanIP listenport=$PublicPort 2>&1 | Out-Null
Write-Host "portproxy removed (if present): ${LanIP}:${PublicPort}" -ForegroundColor Green

# 2. Remove only our own firewall rule by exact display name.
if (Get-NetFirewallRule -DisplayName 'Firebot Web LAN HTTP' -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName 'Firebot Web LAN HTTP'
    Write-Host 'firewall rule removed: Firebot Web LAN HTTP' -ForegroundColor Green
} else {
    Write-Host 'firewall rule not present: Firebot Web LAN HTTP'
}

# 3. Remove only the FIREBOT LAN managed hosts block.
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
[IO.File]::WriteAllLines($hostsPath, $out, [Text.Encoding]::ASCII)
Write-Host 'hosts FIREBOT LAN managed block removed' -ForegroundColor Green

Write-Host ''
Write-Host 'Firebot LAN gateway disabled.' -ForegroundColor Green
Write-Host 'Local dev (http://127.0.0.1:8080) is unaffected.'
