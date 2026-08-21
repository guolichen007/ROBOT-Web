[CmdletBinding()]
param(
    [string]$HostName  = 'firebot.lan',
    [string]$LocalHost = '127.0.0.1',
    [int]   $LocalPort = 8080,
    [string]$LanIP     = '192.168.110.101'
)

# LAN connectivity + WebSocket origin acceptance test. Run on the DEV machine
# after the stack is up (docker compose -f compose.dev.yml up -d) and the LAN
# gateway is enabled (scripts\enable-lan.ps1).
#
# The script verifies the critical fix for "LAN login works but Mock Robot does
# not move": a real WebSocket connect against ws://firebot.lan/ws/v1/monitor
# must reach 101 Switching Protocols, not a 4403 origin rejection.
#
# Mock realtime + patrol/stop/return control still need a manual browser check
# (CASE C/D) — see docs/LAN_WEB_ACCESS.md. This script proves HTTP + API + WS.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

$results = [ordered]@{}

function Test-Http([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 8 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-WebSocket {
    param([string]$WsUri, [string]$Origin)
    $ws = New-Object System.Net.WebSockets.ClientWebSocket
    $ct = [Threading.CancellationToken]::None
    try {
        $ws.Options.SetRequestHeader('Origin', $Origin)
        $ws.ConnectAsync([Uri]$WsUri, $ct).GetAwaiter().GetResult()
    } catch {
        try { $ws.Dispose() } catch { }
        return @{ Connected = $false; Reason = "handshake failed: $($_.Exception.Message)" }
    }
    if ($ws.State -ne [System.Net.WebSockets.WebSocketState]::Open) {
        try { $ws.Dispose() } catch { }
        return @{ Connected = $false; Reason = "unexpected state $($ws.State)" }
    }
    try {
        $buffer = New-Object byte[] 4096
        $seg = New-Object 'System.ArraySegment[byte]' -ArgumentList (, $buffer)
        $recv = $ws.ReceiveAsync($seg, $ct)
        if (-not $recv.Wait(5000)) {
            return @{ Connected = $true; Reason = '101 connected, no frame within 5s (still accepted)' }
        }
        $result = $recv.Result
        if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
            $code = [int]$result.CloseStatus
            $detail = "server closed with code $code"
            if ($code -eq 4403) { $detail += ' (ORIGIN REJECTED)' }
            return @{ Connected = $false; Reason = $detail }
        }
        return @{ Connected = $true; Reason = "101 connected, received $($result.MessageType) frame" }
    } finally {
        try {
            if ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
                $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'test-done', $ct).GetAwaiter().GetResult()
            }
            $ws.Dispose()
        } catch { }
    }
}

Write-Host ''
Write-Host 'Firebot LAN test' -ForegroundColor Cyan
Write-Host ''

# 1. Local dev entry must keep working.
$results['LOCAL_127_HTTP'] = if (Test-Http "http://${LocalHost}:${LocalPort}/health/ready") { 'PASS' } else { 'FAIL' }

# 2. LAN IP over the gateway (port 80).
$results['FIREBOT_LAN_HTTP'] = if (Test-Http "http://${LanIP}/health/ready") { 'PASS' } else { 'FAIL' }

# 3. LAN hostname over the gateway.
$results['FIREBOT_LAN_HOSTNAME_HTTP'] = if (Test-Http "http://${HostName}/health/ready") { 'PASS' } else { 'FAIL' }

# 4. Login page (Vue shell).
$results['FIREBOT_LAN_LOGIN_PAGE'] = if (Test-Http "http://${HostName}/") { 'PASS' } else { 'FAIL' }

# 5+6. API login + ws-ticket, then a real WebSocket connect with the Origin header.
$adminUser = 'admin'
$adminPass = $null
$envPath = Join-Path $repo '.env'
if (Test-Path $envPath) {
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        if ($line -match '^\s*BOOTSTRAP_ADMIN_USERNAME\s*=') { $adminUser = ($line -replace '^\s*BOOTSTRAP_ADMIN_USERNAME\s*=\s*', '').Trim() }
        if ($line -match '^\s*BOOTSTRAP_ADMIN_PASSWORD\s*=') { $adminPass = ($line -replace '^\s*BOOTSTRAP_ADMIN_PASSWORD\s*=\s*', '').Trim() }
    }
}

if (-not $adminPass) {
    $results['FIREBOT_LAN_LOGIN']      = 'SKIP (no admin password in .env)'
    $results['FIREBOT_LAN_API']        = 'SKIP'
    $results['FIREBOT_LAN_WS']         = 'SKIP'
    $results['WS_ORIGIN_ACCEPTED']     = 'NO'
} else {
    $accessToken = $null
    try {
        $body = @{ username = $adminUser; password = $adminPass } | ConvertTo-Json
        $login = Invoke-RestMethod -Uri "http://${HostName}/api/v1/auth/login" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
        $accessToken = $login.access_token
    } catch {
        $accessToken = $null
    }
    $results['FIREBOT_LAN_LOGIN'] = if ($accessToken) { 'PASS' } else { 'FAIL' }

    $ticket = $null
    if ($accessToken) {
        try {
            $headers = @{ Authorization = "Bearer $accessToken"; Origin = "http://${HostName}" }
            $ticketResp = Invoke-RestMethod -Uri "http://${HostName}/api/v1/auth/ws-ticket" -Method Post -Headers $headers -TimeoutSec 10
            $ticket = $ticketResp.ticket
        } catch {
            $ticket = $null
        }
    }
    $results['FIREBOT_LAN_API'] = if ($ticket) { 'PASS' } else { 'FAIL' }

    if ($ticket) {
        $wsResult = Test-WebSocket -WsUri "ws://${HostName}/ws/v1/monitor?ticket=$ticket&after=0-0" -Origin "http://${HostName}"
        if ($wsResult.Connected) {
            $results['FIREBOT_LAN_WS']     = 'PASS'
            $results['WS_ORIGIN_ACCEPTED'] = 'YES'
        } else {
            $results['FIREBOT_LAN_WS']     = 'FAIL'
            $results['WS_ORIGIN_ACCEPTED'] = 'NO'
            Write-Host "WebSocket detail: $($wsResult.Reason)" -ForegroundColor Yellow
        }
    } else {
        $results['FIREBOT_LAN_WS']     = 'SKIP'
        $results['WS_ORIGIN_ACCEPTED'] = 'NO'
    }
}

# Summary.
Write-Host ''
$allPass = $true
foreach ($key in $results.Keys) {
    $val = $results[$key]
    $color = switch ($val) {
        'PASS' { 'Green' }
        'SKIP' { 'Yellow' }
        default { 'Red' }
    }
    Write-Host ("{0,-30} {1}" -f $key, $val) -ForegroundColor $color
    if ($val -ne 'PASS') { $allPass = $false }
}

Write-Host ''
if ($allPass) {
    Write-Host 'LAN_SCRIPT_CHECKS=PASS' -ForegroundColor Green
} else {
    Write-Host 'LAN_SCRIPT_CHECKS=FAIL' -ForegroundColor Red
}
Write-Host ''
Write-Host 'Remaining manual browser checks (CASE C/D, not scriptable here):' -ForegroundColor Yellow
Write-Host "  1. Open http://$HostName and log in."
Write-Host '  2. Confirm Mock R001 is online and its position updates in real time.'
Write-Host '  3. Start a patrol, then stop / return / estop and confirm status updates live.'
Write-Host '  4. F12 -> Network -> WS: the /ws/v1/monitor entry must show 101, not 4403.'
