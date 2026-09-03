#!/usr/bin/env bash
# install-firebotctl.sh — 安装 firebotctl 到 /usr/local/bin（符号链接，任意目录可用）
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SELF")"

if [ "$(id -u)" -eq 0 ]; then
  ln -sf "$SCRIPT_DIR/firebotctl" /usr/local/bin/firebotctl
else
  sudo ln -sf "$SCRIPT_DIR/firebotctl" /usr/local/bin/firebotctl
fi
echo "firebotctl 已安装到 /usr/local/bin/firebotctl"
echo "验证：firebotctl --help"
