[CmdletBinding()]
param()

# Removes the "# BEGIN FIREBOT LAN" ... "# END FIREBOT LAN" hosts block that the
# fallback install script (install-firebot-lan-client.ps1) may have written on a
# demo/client machine, then flushes the DNS cache.
#
# Use this once router DNS (firebot.lan A 192.168.110.101) is configured, so the
# machine resolves via DNS instead of a stale hosts entry. Only the managed block
# is touched; all other hosts lines are preserved.
#
# Run from an elevated PowerShell:
#
#   .\remove-firebot-lan-client.ps1

$ErrorActionPreference = 'Stop'

function Require-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script requires an elevated (Administrator) PowerShell session.'
    }
}

Require-Admin

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

ipconfig /flushdns | Out-Null

Write-Host 'FIREBOT_LAN_CLIENT_REMOVED=YES'
Write-Host 'hosts FIREBOT LAN block removed; DNS cache flushed.'
