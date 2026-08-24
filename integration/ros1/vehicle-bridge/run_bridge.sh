#!/usr/bin/env bash
# Firebot 车端 Bridge 启动脚本（可移植：ROS 路径经环境变量，不硬编码 /home/tl）
# 用法：bash run_bridge.sh [bridge.env 路径]
# 说明：
#   - 密码优先取环境变量（systemd EnvironmentFile 注入）；否则读 /etc/firebot/bridge-secret.env
#   - ROS/catkin setup 路径经 FIREBOT_ROS_SETUP / FIREBOT_ROS_WORKSPACE_SETUP 配置
#   - source 期间关闭 nounset，source 完再启用（第三方脚本可能读未定义变量）
set -eo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 唯一配置位置：/etc/firebot/bridge.env（由 install.sh 生成，systemd 传入）
ENV_FILE="${1:-${FIREBOT_BRIDGE_ENV:-/etc/firebot/bridge.env}}"

# 1) 读取非密配置
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# 2) 密码：环境变量优先，否则从安全文件读取
if [ -z "${FIREBOT_MQTT_PASSWORD:-}" ]; then
  if [ -f /etc/firebot/bridge-secret.env ]; then
    set -a
    # shellcheck disable=SC1090
    . /etc/firebot/bridge-secret.env
    set +a
  fi
fi
if [ -z "${FIREBOT_MQTT_PASSWORD:-}" ]; then
  echo "ERROR: FIREBOT_MQTT_PASSWORD 未设置（/etc/firebot/bridge-secret.env 或 systemd EnvironmentFile）" >&2
  exit 1
fi
export FIREBOT_MQTT_PASSWORD

# 3) source ROS 环境（Noetic + 工作区）；路径可配置，关闭 nounset 避免 setup 读未定义变量退出
set +u
if [ -n "${FIREBOT_ROS_SETUP:-}" ] && [ -f "$FIREBOT_ROS_SETUP" ]; then . "$FIREBOT_ROS_SETUP"; fi
if [ -n "${FIREBOT_ROS_WORKSPACE_SETUP:-}" ] && [ -f "$FIREBOT_ROS_WORKSPACE_SETUP" ]; then . "$FIREBOT_ROS_WORKSPACE_SETUP"; fi
set -u

# 4) 运行 bridge（通信层；不执行任何车辆运动）
# 以包方式运行（-m）以支持 firebot_bridge 包内相对导入
cd "$DIR"
exec python3 -m firebot_bridge.main
