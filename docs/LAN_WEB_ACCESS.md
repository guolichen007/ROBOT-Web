# 局域网 Web 访问（firebot.lan）

开发/演示阶段，同一可信局域网 `192.168.110.0/24` 内的电脑可以直接用浏览器打开
`http://firebot.lan`，不需要 HTTPS、不需要安装证书。

## 概览

| 角色 | 地址 |
| --- | --- |
| 开发机 LAN IP | `192.168.110.101` |
| LAN 网段 | `192.168.110.0/24` |
| 开发机本机入口 | `http://127.0.0.1:8080`（Docker nginx，保持不变） |
| 局域网展示入口 | `http://firebot.lan`（端口 80） |

数据流：

```text
192.168.110.0/24 内其他电脑
        │  http://firebot.lan  (TCP 80)
        ▼
192.168.110.101:80   ← Windows portproxy
        │
        ▼
127.0.0.1:8080       ← Docker nginx
        │
 ┌──────┼────────┐
 Vue   API      WS (WebSocket Upgrade)
```

安全边界：

- 防火墙只允许 `RemoteAddress = 192.168.110.0/24`，从不使用 `Any`。
- 不开放 443、公网、Tailscale Web 或其它子网。
- 不使用 HTTPS、不安装证书。

## 开发机操作

启动栈（与以往相同）：

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
```

本机始终可用：

```text
http://127.0.0.1:8080
```

开启局域网入口（只需一次，需要管理员 PowerShell）：

```powershell
.\scripts\enable-lan.ps1
```

该脚本会：

1. 校验 `192.168.110.101` 确实存在于本机网卡；
2. 保证 `iphlpsvc` 服务运行且为自动启动；
3. 检查 80 端口是否被其它进程占用（占用则打印 PID 并退出，绝不杀进程）；
4. 建立（幂等）`192.168.110.101:80 → 127.0.0.1:8080` 的 portproxy；
5. 建立（幂等）防火墙规则 `Firebot Web LAN HTTP`（仅 `192.168.110.0/24`）；
6. 幂等写入开发机 hosts 的 `# BEGIN FIREBOT LAN` 托管块；
7. 把 `http://firebot.lan` 安全地加入 `.env` 的 `ALLOWED_ORIGINS`（不改动任何 secret）。

> 重要：`ALLOWED_ORIGINS` 修改后，必须重建 api 容器才能让新配置生效
> （`docker compose` 的 `env_file` 只在重建容器时重新读取）：
>
> ```powershell
> docker compose -f compose.dev.yml up -d --force-recreate api
> ```
>
> `enable-lan.ps1 -RestartApi` 可以顺带执行这一步。

## 展示电脑操作（只做一次）

在同一个 `192.168.110.0/24` 网段的展示电脑上，以管理员 PowerShell 运行一次：

```powershell
.\install-firebot-lan-client.ps1
```

该脚本会：

1. 校验管理员权限；
2. `Test-NetConnection 192.168.110.101 -Port 80` 确认网关可达；
3. 幂等写入 hosts 的 `# BEGIN FIREBOT LAN` 托管块（`192.168.110.101 firebot.lan`）；
4. `ipconfig /flushdns`；
5. 校验 `firebot.lan` 解析到 `192.168.110.101`；
6. 校验 `http://firebot.lan/health/ready` 返回 `200 ok=true`。

输出 `FIREBOT_LAN_CLIENT_READY=YES` 即成功。之后永远直接：

```text
http://firebot.lan
```

展示电脑不需要 Docker、Node、项目源码、Tailscale 或证书，只要浏览器。

## 验收

在开发机上运行（栈已启动、LAN 网关已开启之后）：

```powershell
.\scripts\test-lan.ps1
```

脚本会依次验证：`127.0.0.1:8080`、`192.168.110.101`、`firebot.lan` 的
`/health/ready`、登录页、API 登录与 `ws-ticket`，并用真实票据建立
`ws://firebot.lan/ws/v1/monitor` 的 WebSocket 连接，确认握手为 `101 Switching
Protocols` 而不是 `4403`。

脚本之外的浏览器人工验收（CASE C/D）：

1. 在 `http://firebot.lan` 登录；
2. 确认 Mock `R001` 在线、地图位置实时变化；
3. 开始巡检 → 停止 / 返航 / 急停，确认状态实时更新；
4. 按 F12 → Network → WS，`/ws/v1/monitor` 应为 `101`，Console 无重连循环。

## 关闭局域网入口

```powershell
.\scripts\disable-lan.ps1
```

只删除 Firebot 自己建立的 portproxy 规则、防火墙规则和 hosts 托管块，不影响
`127.0.0.1:8080` 本机开发，也绝不清理其它 portproxy / 防火墙 / hosts 行。

## 为什么之前 18080 能登录但模拟车不动

前端 WebSocket 使用 `location.host` 动态同源，这本身是正确设计。问题在后端：
`app/core/websocket.py` 会校验 `Origin` 头，不在 `ALLOWED_ORIGINS` 里就
`close(code=4403)`。此前 DEV 默认只允许 `localhost:8080`、`127.0.0.1:8080`、
`nginx`，没有 `firebot.lan`（也没有 `192.168.110.101:18080`），所以登录/API
正常，但 WebSocket 被 4403 拒绝，模拟车实时动画和实时任务状态全部中断。

本轮把 `http://firebot.lan` 加入 `ALLOWED_ORIGINS`（`.env.example` 与现有
`.env`），并保留前端的动态同源与后端的 Origin 校验，不放开 `allow all`。

## 未来 DNS（预留）

当前使用 hosts 是临时开发/展示方案。以后可以在路由器、dnsmasq、OpenWrt 或正式
DNS 里建立：

```text
firebot.lan  A  192.168.110.101
```

届时展示电脑无需再运行 hosts 脚本。本轮不在当前 Gate 内部署 DNS 服务。
