# Vehicle Bridge 现场运维交接（HANDOFF）

> 这是**运维交接资产**，不是 Bridge runtime `13c8692` 的一部分。
> 本目录里的 SHA 固定为批准 runtime，绝不随 Server/Web 开发支线 HEAD 前移。

## 当前正式日常运行方式

```text
systemd
  ↓
firebot-bridge.service
  ↓
python3 -m firebot_bridge.main
```

**不是**直接 `run_bridge.sh`（那是 foreground 调试用）。

## 现场常用命令

```bash
# 状态（只读）
HANDOFF/bin/bridge-status.sh

# 大屏（每秒刷新，Ctrl+C 只退出 viewer）
HANDOFF/bin/bridge-dashboard.sh

# 启动（带安全 Gate）
sudo HANDOFF/bin/bridge-start.sh

# 停止（graceful，只用 systemctl）
sudo HANDOFF/bin/bridge-stop.sh

# 快照（非 secret，写入 logs/）
HANDOFF/bin/bridge-snapshot.sh
```

## 最重要的一条

如果 `firebot-bridge.service` 处于 `active`，**禁止**再启动第二个 Bridge（相同 MQTT identity 会互相踢线）。

## 目录

| 文件 | 说明 |
| --- | --- |
| `README.md` | 本页 |
| `CURRENT_STATE.md` | 当前首车现场运行形态与 Gate |
| `RUNBOOK.md` | 启停/状态/排障 |
| `SAFETY.md` | 安全边界 |
| `NEXT_PHASE.md` | 下一阶段（Phase E1，需另行批准） |
| `APPROVED_RUNTIME.txt` | 批准 runtime SHA |
| `bin/*.sh` | 现场运维脚本 |
