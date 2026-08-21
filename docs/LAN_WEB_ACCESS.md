# 局域网 Web 访问（firebot.lan）

开发/演示阶段，同一个局域网内的任何电脑接入网络后，直接用浏览器打开
`http://firebot.lan` 即可，不需要安装证书、不需要运行任何脚本。

正式方案是**路由器 DNS**；`install-firebot-lan-client.ps1` 只是 DNS 未配好时的
应急 fallback，不作为正式部署流程。

## 目标体验

```text
拿一台新 Windows 笔记本
        ↓
插网线 / 连现场 Wi-Fi
        ↓
DHCP 获得 192.168.110.xxx
        ↓
打开 Chrome
        ↓
http://firebot.lan
        ↓
登录
```

仅此而已。客户端不需要 Git、Claude、PowerShell 脚本、Docker、Node、
Tailscale、证书，也不需要在 hosts 里写任何东西。

## 概览

```text
            192.168.110.1（路由器 / DNS）
                    │  DNS: firebot.lan = 192.168.110.101
                    │  DHCP 下发该 DNS 给所有客户端
       ┌────────────┼────────────┐
       ▼            ▼            ▼
  192.168.110.20  192.168.110.30  192.168.110.80    （任意客户端，浏览器）
       └────────────┼────────────┘
                    │  http://firebot.lan  (TCP 80)
                    ▼
            192.168.110.101（开发机）
                    │  Windows portproxy
                    ▼
            127.0.0.1:8080（Docker nginx）
                    │
        ┌───────────┼───────────┐
       Vue        API         WS (WebSocket Upgrade)
```

安全边界不变：

- 防火墙只允许 `RemoteAddress = 192.168.110.0/24`，从不使用 `Any`。
- 不开放 443、公网、Tailscale Web 或其它子网。
- 不使用 HTTPS、不安装证书。

## 正式模式：路由器 DNS（推荐，客户端零安装）

只需要在路由器（`192.168.110.1`，若你的网关/DNS 地址不同以实际为准）上配一条
记录：

```text
firebot.lan    A    192.168.110.101
```

不同路由器界面里的叫法可能不同，本质都是这一条：

```text
本地 DNS / 静态 DNS / DNS Host / Host Records / 域名绑定 /
DNS 重写 / Local DNS Records / 域名解析
```

如果是 OpenWrt，用 dnsmasq 最简单，逻辑上就是：

```text
address=/firebot.lan/192.168.110.101
```

可以写在 `/etc/dnsmasq.conf`，或在 LuCI 的
`Network → DHCP and DNS` 里添加自定义配置 / 域名绑定。随后确认 DHCP 给客户端
下发的是这台路由器的 DNS（通常默认就是）。

配好之后，任何电脑只要：

1. 接入 `192.168.110.x` 网络；
2. DNS 由路由器下发；
3. 浏览器输入 `http://firebot.lan`。

即可，什么脚本都不用装。

### 验证 DNS 已生效（在任意一台客户端上）

在没有写 hosts 的客户端上执行：

```powershell
Resolve-DnsName firebot.lan
```

应解析到 `192.168.110.101`。然后浏览器打开 `http://firebot.lan` 能出现登录页
即说明 DNS 生效（而非本地 hosts）。

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
6. 幂等写入开发机 hosts 的 `# BEGIN FIREBOT LAN` 托管块（保证开发机自己也能解析
   `firebot.lan`，即使路由器 DNS 尚未配置）；
7. 把 `http://firebot.lan` 安全地加入 `.env` 的 `ALLOWED_ORIGINS`（不改动任何 secret）。

> 重要：`ALLOWED_ORIGINS` 修改后，必须重建 api 容器才能让新配置生效
> （`docker compose` 的 `env_file` 只在重建容器时重新读取）：
>
> ```powershell
> docker compose -f compose.dev.yml up -d --force-recreate api
> ```
>
> `enable-lan.ps1 -RestartApi` 可以顺带执行这一步。

## 应急模式：hosts 脚本（仅当 DNS 未配好）

```powershell
.\install-firebot-lan-client.ps1
```

这是 **fallback**，用于现场路由器 DNS 还没配好、又需要立刻让某台电脑访问时，
临时给它写一条 hosts 记录。它不是正式部署流程。

该脚本会（管理员 PowerShell 下）：

1. 校验管理员权限；
2. `Test-NetConnection 192.168.110.101 -Port 80` 确认网关可达；
3. 幂等写入 hosts 的 `# BEGIN FIREBOT LAN` 托管块（`192.168.110.101 firebot.lan`）；
4. `ipconfig /flushdns`；
5. 校验 `firebot.lan` 解析到 `192.168.110.101`；
6. 校验 `http://firebot.lan/health/ready` 返回 `200 ok=true`。

DNS 配好后，客户端不再需要保存任何 Firebot 文件，也不应有 hosts 残留。

## 验收

在开发机上运行（栈已启动、LAN 网关已开启之后）：

```powershell
.\scripts\test-lan.ps1 -Password <admin 当前密码>
```

脚本会依次验证：`127.0.0.1:8080`、`192.168.110.101`、`firebot.lan` 的
`/health/ready`、登录页、API 登录与 `ws-ticket`，并用真实票据建立
`ws://firebot.lan/ws/v1/monitor` 的 WebSocket 连接，确认握手为 `101 Switching
Protocols` 而不是 `4403`，最终输出 `WS_ORIGIN_ACCEPTED=YES`。

脚本之外的浏览器人工验收（在一台走 DNS 的客户端上）：

1. 在 `http://firebot.lan` 登录；
2. 确认 Mock `R001` 在线、地图位置实时变化；
3. 开始巡检 → 停止 / 返航 / 急停，确认状态实时更新；
4. 按 F12 → Network → WS，`/ws/v1/monitor` 应为 `101`，Console 无重连循环。

## 关闭局域网入口

```powershell
.\scripts\disable-lan.ps1
```

只删除 Firebot 自己建立的 portproxy 规则、防火墙规则和开发机 hosts 托管块，不影响
`127.0.0.1:8080` 本机开发，也绝不清理其它 portproxy / 防火墙 / hosts 行。
路由器上的 DNS 记录需在路由器界面里单独删除。

## 为什么之前 18080 能登录但模拟车不动

前端 WebSocket 使用 `location.host` 动态同源，这本身是正确设计。问题在后端：
`app/core/websocket.py` 会校验 `Origin` 头，不在 `ALLOWED_ORIGINS` 里就
`close(code=4403)`。此前 DEV 默认只允许 `localhost:8080`、`127.0.0.1:8080`、
`nginx`，没有 `firebot.lan`（也没有 `192.168.110.101:18080`），所以登录/API
正常，但 WebSocket 被 4403 拒绝，模拟车实时动画和实时任务状态全部中断。

本轮把 `http://firebot.lan` 加入 `ALLOWED_ORIGINS`（`.env.example` 与现有
`.env`），并保留前端的动态同源与后端的 Origin 校验，不放开 `allow all`。
