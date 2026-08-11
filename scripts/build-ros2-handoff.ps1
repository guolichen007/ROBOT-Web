[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
python scripts/generate_ros2_handoff.py --dist
if ($LASTEXITCODE -ne 0) { throw 'ROS2 handoff build failed.' }
