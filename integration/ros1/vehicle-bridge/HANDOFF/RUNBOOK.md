# 现场运维 Runbook

## 启动

```bash
sudo HANDOFF/bin/bridge-start.sh
```

start Gate 会检查：

```text
/etc/firebot/bridge.env exists
/etc/firebot/bridge-secret.env exists（只查存在，不读内容）
/etc/firebot/production-ca.crt exists
BRIDGE_STUB_MODE=false（只解析白名单 key，不 source 整个 env）
FIREBOT_SUPPORTED_COMMANDS=
FIREBOT_SENSORS=
FIREBOT_LOCATION_ENABLED=false
effective systemd（systemctl show）：User=tl、WorkingDirectory=/home/tl/vehicle-bridge、
  ExecStart 含 python3 -m firebot_bridge.main、Environment 含 ROS_MASTER_URI=http://127.0.0.1:1
启动后等待 active + MainPID>0 + mqtt_connected=true，任一不满足 → BRIDGE_START=FAIL
```

不满足任何一项 → `STOP`，不启动。

## 停止

```bash
sudo HANDOFF/bin/bridge-stop.sh
```

只用 `systemctl stop`。禁止 `kill -9` / `pkill` / `killall`。

## 状态

```bash
HANDOFF/bin/bridge-status.sh
HANDOFF/bin/bridge-dashboard.sh   # 每秒刷新，Ctrl+C 退出 viewer
```

## 排障顺序

1. `systemctl status firebot-bridge`
2. `HANDOFF/bin/bridge-status.sh`
3. `sudo journalctl -u firebot-bridge -n 100 --no-pager`

## 禁止

- service active 时再启动第二个 Bridge。
- 现场人员自行修改：`bridge.env` / `bridge-secret.env` / CA / systemd unit。
- 现场覆盖已有 CA / secret。

任何配置变化需 owner approval。
