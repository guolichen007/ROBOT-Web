# 当前首车现场状态

> 历史快照：本文件保留 2026-08-31 现场验证证据（不改写历史 PASS）。
> 当前状态唯一真相源已迁移到 [`docs/现场状态/当前现场状态.md`](../../../../docs/现场状态/当前现场状态.md)。

## 版本

```text
批准基线 = integration/server-web-real-vehicle-ready-v1 HEAD（以 git rev-parse 为准，不硬编码 SHA）
安装目录 = /opt/firebot/vehicle-bridge（正式运行副本，install.sh 原子切换 + APPROVED_RUNTIME.txt 留痕）
```

## 现场运行形态（firebot-vehicle-02 现场验证）

```text
VEHICLE_OS=Ubuntu 20.04
ROS=/opt/ros/noetic
ROS 工作区=/home/tl/firerobot_ws
Bridge=systemd firebot-bridge.service（run_bridge.sh /etc/firebot/bridge.env）
控制 adapter=firebot_control_adapter（订阅 /firebot_bridge/command，PATROL_START fail-closed）
Status=/run/firebot-bridge/status.json
Env=/etc/firebot/bridge.env
Secret=/etc/firebot/bridge-secret.env
```

## 2026-08-31 R1 现场验证

```text
Bridge communication              = PASS
MQTT downlink                     = PASS
PATROL_START software path        = PASS
Bridge → ROS                      = PASS
firebot_control_adapter feedback  = PASS
fail-closed NAV_EXECUTION_NOT_READY = PASS
vehicle watch                     = PASS
event audit                       = PASS
command trace                     = PASS
服务器 ACK 接收                    = PASS

REAL_MOTION_VERIFIED              = NO
NAVIGATION_EXECUTION_VERIFIED     = NO
REAL_BATTERY_VERIFIED             = NO
LOCATION_PROVIDER_VERIFIED        = NO

source_kind = ROS_COMPAT（不变，未迁移）
```

> `PATROL_START software path = PASS` 只表示「服务器→MQTT→Bridge→ROS→adapter→REJECTED(NAV_EXECUTION_NOT_READY)→ACK」下行信号链已打通且 fail-closed 正确拒绝。
> **不**表示真实巡检/真实运动已执行（当时真实导航未就绪，被正确拒绝）。

## 2026-08-31 U2 Telemetry Freshness 现场验证

```text
VEHICLE_BATTERY_FRESHNESS              = PASS
VEHICLE_SMOKE_FRESHNESS                = PASS
STALE_VALUE_REPUBLISH_FIXED_ON_VEHICLE = PASS
FRESH_STALE_RECOVERED_FIELD_TEST       = PASS
smoke_provider_seen 现场修复           = PASS（已回收）

REAL_BATTERY_VERIFIED                  = NO
REAL_SMOKE_VERIFIED                    = NO
REAL_BATTERY_SOURCE                    = NOT_FOUND
REAL_SMOKE_SOURCE                      = NOT_IMPLEMENTED
NAVIGATION_EXECUTION_VERIFIED          = NO
REAL_MOTION_VERIFIED                   = NO
```

> freshness 依据 = 消息是否持续到达（monotonic），不是数值是否变化。
> TTL=5s 是 firebot-vehicle-02 U2 测试值，不是生产 TTL；生产 TTL = PENDING_REAL_PROVIDER_RATE。

## 能力范围

```text
FIREBOT_SUPPORTED_COMMANDS=patrol（仅 patrol；firebot-vehicle-02 现场配置，非产品默认）
其它控制能力（manual/stop/estop/reset/return/extinguish/cancel）= 未开放
CONTROL_CODE=PATROL_START,STOP_MOTION、CONTROL_FIELD_VERIFIED=NO（真实运动仍未验收）
```

PID / boot_id 是运行时易变值，固定身份只有 APPROVED_RUNTIME.txt 记录的来源 SHA。
