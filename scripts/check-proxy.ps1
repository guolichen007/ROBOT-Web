$ErrorActionPreference = "Stop"
$internet = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$configured = [bool]$internet.ProxyEnable -or [bool]$env:HTTPS_PROXY -or [bool]$env:https_proxy
Write-Host "Windows/环境代理已配置：$configured"
Write-Host "注意：为避免泄露凭据，本脚本不会打印代理 URL。"
try {
  $response = Invoke-WebRequest -Uri "https://registry-1.docker.io/v2/" -TimeoutSec 10 -UseBasicParsing
  Write-Host "Docker Registry HTTPS 可达：HTTP $($response.StatusCode)" -ForegroundColor Green
} catch {
  if ($_.Exception.Response.StatusCode.value__ -eq 401) {
    Write-Host "Docker Registry HTTPS 可达（匿名请求返回 401，符合预期）。" -ForegroundColor Green
  } else {
    Write-Host "Docker Registry HTTPS 不可达；请在 Docker Desktop 中检查代理配置。" -ForegroundColor Yellow
  }
}
