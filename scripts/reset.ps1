[CmdletBinding()]
param([switch]$ConfirmReset)

$ErrorActionPreference = 'Stop'
if (-not $ConfirmReset) { throw 'This deletes Firebot DEV volumes. Pass -ConfirmReset explicitly.' }
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$project = docker compose -f compose.dev.yml ls --format json | ConvertFrom-Json
docker compose -f compose.dev.yml down --volumes --remove-orphans
Write-Host 'Only Firebot DEV containers, network, and volumes were removed. Repository files were preserved.'
