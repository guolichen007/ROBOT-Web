#!/usr/bin/env bash
# Firebot 车端 Bridge 卸载脚本：停止并移除 systemd unit；默认保留安装目录与 secret。
set -euo pipefail

INSTALL_DIR="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"

echo "== Firebot Bridge 卸载 =="
sudo systemctl disable --now firebot-bridge 2>/dev/null || true
sudo rm -f /etc/systemd/system/firebot-bridge.service
sudo systemctl daemon-reload

if [ "${1:-}" = "--purge" ]; then
  echo "  删除安装目录: $INSTALL_DIR"
  sudo rm -rf "$INSTALL_DIR"
else
  echo "  保留安装目录: $INSTALL_DIR（如需删除：uninstall.sh --purge）"
fi

echo "  注意：/etc/firebot/bridge-secret.env 与 bridge.env 不会被删除。"
