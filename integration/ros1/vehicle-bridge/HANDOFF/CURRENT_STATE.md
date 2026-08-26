# 当前首车现场状态（2026-08-26）

## 版本

```text
批准 runtime = 13c869247079b88da11b36666755906001a0041c
服务器现场   = 41bbaf4398711fd940dde1818193a67d34e355c8
```

## 现场运行形态（Bridge-only 隔离）

```text
VEHICLE_OS=Ubuntu 20.04
BRIDGE_FIELD_PATH=/home/tl/vehicle-bridge
SYSTEMD_UNIT=/etc/systemd/system/firebot-bridge.service
WorkingDirectory=/home/tl/vehicle-bridge
ExecStart=/usr/bin/python3 -m firebot_bridge.main
ROS_MASTER_URI=http://127.0.0.1:1
Status=/run/firebot-bridge/status.json
Env=/etc/firebot/bridge.env
Secret=/etc/firebot/bridge-secret.env
```

## 安全态

```text
BRIDGE_STUB_MODE=false
FIREBOT_SUPPORTED_COMMANDS=
FIREBOT_SENSORS=
FIREBOT_LOCATION_ENABLED=false
REAL_CONTROL=NOT_IMPLEMENTED
```

## 已完成 Gate

```text
Bridge communication = PASS
Bridge operation      = PASS
Broker reconnect      = PASS
Graceful stop         = PASS
LWT                   = PASS
Systemd recovery      = PASS
Short soak            = PASS
Long soak             = DEFERRED
Web UI                = NOT_CHECKED
ROS Provider          = NOT_STARTED
source_kind migration = NOT_STARTED
```

## 最近观察到的运行时值（易变，非固定预期）

```text
LAST_OBSERVED_VEHICLE_PID=942282
LAST_OBSERVED_BOOT_ID=98e2d92b-0c47-4594-9751-00ee061b511f
LAST_OBSERVED_NRESTARTS=1
```

PID / boot_id 是运行时易变值，固定身份只有 `13c8692`。
