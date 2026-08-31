# 当前首车现场状态（2026-08-31）

## 版本

```text
批准 runtime = 169dcc34678f487c5f5b425cfcbb4ca8275bf972（2026-08-31 现场验证代码）
服务器现场   = 41bbaf4398711fd940dde1818193a67d34e355c8
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

## 能力范围

```text
FIREBOT_SUPPORTED_COMMANDS=patrol（仅 patrol；firebot-vehicle-02 现场配置，非产品默认）
其它控制能力（manual/stop/estop/reset/return/extinguish/cancel）= 未开放
REAL_CONTROL=NOT_IMPLEMENTED（真实运动仍未实现/未验证）
```

PID / boot_id 是运行时易变值，固定身份只有 `169dcc3`。
