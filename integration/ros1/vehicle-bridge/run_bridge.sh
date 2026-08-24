#!/usr/bin/env bash
# Firebot 车端 Bridge 启动脚本（修复 systemd 常驻的 set -u 根因）
# 用法：bash run_bridge.sh [bridge.env 路径]
# 说明：
#   - 密码优先取环境变量（systemd EnvironmentFile 注入）；否则读 /etc/firebot/vehicle-mqtt-password
#   - ROS/catkin setup 期间关闭 nounset，source 完再启用（第三方脚本可能读未定义变量）
set -eo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$DIR/config/bridge.env}"

# 1) 读取非密配置
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# 2) 密码：环境变量优先，否则从安全文件读取
if [ -z "${FIREBOT_MQTT_PASSWORD:-}" ]; then
  if [ -f /etc/firebot/vehicle-mqtt-password ]; then
    FIREBOT_MQTT_PASSWORD="$(cat /etc/firebot/vehicle-mqtt-password | tr -d '\n')"
  else
    echo "ERROR: FIREBOT_MQTT_PASSWORD 未设置（systemd EnvironmentFile 或 /etc/firebot/vehicle-mqtt-password）" >&2
    exit 1
  fi
fi
export FIREBOT_MQTT_PASSWORD

# 3) source ROS 环境（Noetic + 工作区）；关闭 nounset 避免 setup 读未定义变量退出
set +u
if [ -f /opt/ros/noetic/setup.bash ]; then . /opt/ros/noetic/setup.bash; fi
if [ -f /home/tl/firerobot_ws/devel/setup.bash ]; then . /home/tl/firerobot_ws/devel/setup.bash; fi
set -u

# 4) 运行 bridge（通信层；不执行任何车辆运动）
# 以包方式运行（-m）以支持 firebot_bridge 包内相对导入
cd "$DIR"
exec python3 -m firebot_bridge.main
