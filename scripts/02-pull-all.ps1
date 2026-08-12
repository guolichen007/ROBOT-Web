param([string]$OutputDirectory = "C:\firebot-offline-bundle")

$ErrorActionPreference = "Stop"
$images = @(
  "python:3.12.13-alpine3.24",
  "node:24.19.0-alpine3.24",
  "nginx:1.30.4-alpine",
  "busybox:1.37.0-musl",
  "postgres:18.4-alpine",
  "redis:8.4.5-alpine",
  "eclipse-mosquitto:2.1.2-alpine",
  "bluenviron/mediamtx:1.18.2"
)

$resolvedOutput = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
foreach ($image in $images) {
  docker pull --platform linux/amd64 $image | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "镜像拉取失败：$image" }
}

$archive = Join-Path $resolvedOutput "firebot-base-images-linux-amd64.tar"
docker save -o $archive $images
if ($LASTEXITCODE -ne 0) { throw "Docker 镜像归档创建失败" }
$images | Set-Content -Encoding utf8 (Join-Path $resolvedOutput "source-images.manifest")
docker version --format '{{json .}}' | Set-Content -Encoding utf8 (Join-Path $resolvedOutput "source-docker-version.json")
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
"$hash  firebot-base-images-linux-amd64.tar" | Set-Content -Encoding ascii (Join-Path $resolvedOutput "SHA256SUMS.txt")
Write-Host "离线镜像包已创建：$resolvedOutput" -ForegroundColor Green
