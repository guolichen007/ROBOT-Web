# 服务器与 Web 现场配合

> 面向服务器 / Web 现场人员。**当前已经运行的服务器**与“新服务器从零部署”是两件事：
> 从零部署见 [Ubuntu服务器部署.md](Ubuntu服务器部署.md)；本文件是**当前现场服务器**在实车接入阶段的唯一操作依据。
> 状态真相源见 [实车现场联调总览.md](实车现场联调总览.md)。

---

## 0. 当前阶段与版本双轨

Completed：

```text
Bridge initial connect
broker reconnect
graceful stop
systemd
LWT
recovery
short soak
```

Current：

```text
Phase E0 completed / Phase E1 preparing
```

版本（当前批准基线）：

```text
DEVELOPMENT_BRANCH=integration/server-web-real-vehicle-ready-v1
批准 SHA = 分支 HEAD（git rev-parse HEAD，不硬编码）
部署校验 = FIREBOT_REQUIRE_SHA=<HEAD SHA>
```

> 历史基线（`41bbaf4` / `8e63d5d`）见历史交接文档，不再作为当前批准基线。

---

## 1. 当前服务器基线

```text
目录：/opt/firebot/ROBOT-Web
分支：integration/server-web-real-vehicle-ready-v1
批准 SHA：以分支 HEAD 为准（不硬编码）
```

先确认：

```bash
cd /opt/firebot/ROBOT-Web
git rev-parse HEAD   # 记录当前 SHA，用于部署校验与回滚
```

---

## 2. 现场期间：禁止 / 允许（R0–R4 已完成，纪律按批准流程演进）

禁止：

```text
source_kind migration
控制 flag 修改（FIREBOT_SUPPORTED_COMMANDS 不开放）
secret 重建
CA 重签 / 替换
网络 / Tailscale / firewall 修改
```

允许（J5 已 pull+deploy、J6-S1 按批准流程受控执行）：

```text
git fetch + git pull --ff-only 到批准 SHA（先记录/确认 SHA）
只部署批准变更的服务（J6-S1 仅 task-worker，不整体重造）
数据库 migration（仅随批准代码，且先备份）
只读验证（readonly verify）
服务健康检查
MQTT 观察
DB / API 观察
Web 观察
owner 批准的 broker 故障注入
```

---

## 3. Server R0

确认：

```text
vehicle online 可见
capabilities 已收到
commands=[]
sensors=[]
```

正常 Web / API 因为 capability 不支持而阻止真实 patrol——**不得为了测试硬把命令发下去**。
若专门做 Bridge unsupported transport 测试，必须写成**受控 MQTT 测试**，不能描述成正常 Web 操作。

---

## 4. Server R1 / R2

观察：

```text
MQTT heartbeat 持续
同一 boot_id 不变
同一 Bridge 进程生命周期不变
```

ROS 是否存在**不应**决定 MQTT online。

---

## 5. Server R3（broker restart）

```text
OWNER_APPROVAL_REQUIRED
```

MQTT service 名为 `mosquitto`（`docker-compose.server.yml`）。受控重启前必须由 owner 批准：

```bash
cd /opt/firebot/ROBOT-Web
docker compose \
  --env-file .env.server \
  -f docker-compose.server.yml \
  restart mosquitto
```

重启后立即确认：

```bash
docker compose \
  --env-file .env.server \
  -f docker-compose.server.yml \
  ps mosquitto mqtt-ingress command-dispatcher
```

> 车端人员不得私自重启服务器 Mosquitto。

---

## 6. Server R4（battery=67.5 链路）

```text
车端手工发布 67.5
        ↓
Bridge MQTT
        ↓
mqtt-ingress
        ↓
DB / event stream
        ↓
REST / WebSocket
        ↓
Web
```

服务器端负责证明：

```text
SERVER_BATTERY_RECEIVED=YES
WEB_BATTERY_UPDATED=YES
```

但：

```text
REAL_BATTERY_PROVIDER=NOT_VERIFIED
```

---

## 7. Web 当前边界

本轮验证：

```text
登录
REST snapshot
ws-ticket
WSS monitor
online / offline
battery 实时更新
数据 stale / reconnect 表现
```

本轮**不**验证：

```text
真实 patrol
真实 emergency stop
真实 extinguish
真实 manual control
真实视频
真实 location
```

原因：`supported_commands=[]`、`location_enabled=false`、`real control not implemented`。

Web 即使已存在控制 UI，也**不能**把“按钮存在”等价成“真车能力已完成”；所有控制能力必须继续 capability / readiness gated。
