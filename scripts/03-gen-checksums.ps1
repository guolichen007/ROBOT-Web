param([Parameter(Mandatory = $true)][string]$BundleDirectory)

$ErrorActionPreference = "Stop"
$resolvedBundle = [IO.Path]::GetFullPath($BundleDirectory)
$archive = Join-Path $resolvedBundle "firebot-base-images-linux-amd64.tar"
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "找不到镜像归档：$archive" }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
"$hash  firebot-base-images-linux-amd64.tar" | Set-Content -Encoding ascii (Join-Path $resolvedBundle "SHA256SUMS.txt")
Write-Host "校验文件已更新。" -ForegroundColor Green
