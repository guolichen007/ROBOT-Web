param([string]$Image = "busybox:1.37.0-musl")

$ErrorActionPreference = "Stop"
docker version | Out-Host
docker pull --platform linux/amd64 $Image | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Docker 无法拉取固定版本镜像：$Image" }
Write-Host "Docker Engine 与镜像仓库连接正常。" -ForegroundColor Green
