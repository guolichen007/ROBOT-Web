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
BRIDGE_STUB_MODE=false
FIREBOT_SUPPORTED_COMMANDS=
FIREBOT_SENSORS=
FIREBOT_LOCATION_ENABLED=false
unit ExecStart = python3 -m firebot_bridge.main
unit ROS_MASTER_URI = http://127.0.0.1:1
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
- 修改 `/etc/firebot/bridge-secret.env` 之外的环境变量后不重启。
- 现场覆盖已有 CA / secret。
