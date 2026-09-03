#!/usr/bin/env bash
# firebotctl 路径解析测试：repo 根 / 任意目录 / 符号链接 三种调用都能正确定位 ROBOT-Web 根。
set -euo pipefail

ROS1_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIREBOTCTL="$ROS1_DIR/firebotctl"
PASS=0
FAIL=0

check() {
  local name="$1"
  shift
  if "$@"; then
    PASS=$((PASS + 1))
    echo "  [PASS] $name"
  else
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $name"
  fi
}

# 1) repo 根目录调用 --help
check "repo 根目录调用 --help" bash -c "cd '$ROS1_DIR/..' && bash '$FIREBOTCTL' --help >/dev/null 2>&1"

# 2) /tmp 目录调用（不依赖 cwd）
check "/tmp 目录调用 --help" bash -c "cd /tmp && bash '$FIREBOTCTL' --help >/dev/null 2>&1"

# 3) 符号链接调用（模拟 /usr/local/bin/firebotctl）
TMPLINK="$(mktemp -d)/firebotctl"
ln -s "$FIREBOTCTL" "$TMPLINK"
check "符号链接调用 --help" bash -c "cd /tmp && bash '$TMPLINK' --help >/dev/null 2>&1"

# 4) 真实路径解析：__pathcheck__ 必须找到 ROBOT-Web 根的 scripts/server-deploy.sh
check "repo 根路径解析（__pathcheck__）" bash -c "cd /tmp && bash '$FIREBOTCTL' __pathcheck__ >/dev/null 2>&1"
check "符号链接路径解析（__pathcheck__）" bash -c "cd /tmp && bash '$TMPLINK' __pathcheck__ >/dev/null 2>&1"
rm -rf "$(dirname "$TMPLINK")"

echo ""
echo "结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
