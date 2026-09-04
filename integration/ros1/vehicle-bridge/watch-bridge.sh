#!/usr/bin/env bash
# Firebot 车端 Bridge 现场实时控制台（观察者）。
#
# 只观察 systemd 运行的 Bridge；绝不启动第二个 Bridge、绝不 restart service、
# 绝不 source secret、绝不修改配置。Ctrl+C 只退出本 viewer，Bridge 继续运行。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS="${FIREBOT_BRIDGE_STATUS_FILE:-/run/firebot-bridge/status.json}"

echo "Firebot Bridge live viewer"
echo "Ctrl+C exits viewer only. Bridge service keeps running."
echo

SERVICE="$(systemctl is-active firebot-bridge 2>/dev/null || true)"
if [[ "$SERVICE" != "active" ]]; then
    echo "ERROR: firebot-bridge service is not active: ${SERVICE}" >&2
    echo "Start the service before using the live viewer." >&2
    exit 2
fi

# 单一事实源：events.jsonl。
# 历史回放最近 15 条 + 实时跟随（tail -F 跨轮转跟随，天然无重复）。
# journalctl 仅作为技术诊断日志，不再作为正常 watch 数据源。
EVENTS_FILE="${FIREBOT_EVENTS_DIR:-$SELF_DIR/logs}/events.jsonl"
tail -n 15 -F "$EVENTS_FILE" 2>/dev/null \
| python3 "$SELF_DIR/tools/field_console.py" --jsonl \
    --status-file "$STATUS" --lang zh \
    "$@"
