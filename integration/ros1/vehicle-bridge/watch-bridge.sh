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
    echo "WARNING: firebot-bridge service is not active: ${SERVICE}"
    echo
fi

journalctl \
    -n 20 \
    -fu firebot-bridge \
    -o cat \
| python3 "$SELF_DIR/tools/field_console.py" \
    --status-file "$STATUS" \
    "$@"
