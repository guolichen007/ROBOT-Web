#!/usr/bin/env bash
# Firebot 车端 Bridge 安装脚本（GitHub 是唯一交付源；本脚本把代码装到固定目录并生成 systemd）
#
# 可配置（环境变量，均有默认值）：
#   FIREBOT_INSTALL_DIR        安装目录（默认 /opt/firebot/vehicle-bridge）
#   FIREBOT_BRIDGE_USER        systemd 运行用户（默认当前用户）
#   FIREBOT_ROS_SETUP          ROS setup.bash（默认 /opt/ros/noetic/setup.bash）
#   FIREBOT_ROS_WORKSPACE_SETUP 车端工作区 setup.bash（默认空）
#   FIREBOT_BRIDGE_ENV         车端 bridge.env 路径（默认 /etc/firebot/bridge.env）
#
# 边界：本脚本只安装通信层，不创建/改变任何 ROS 控制逻辑；不自动写入生产密码。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"
BRIDGE_USER="${FIREBOT_BRIDGE_USER:-$(id -un)}"
ROS_SETUP="${FIREBOT_ROS_SETUP:-/opt/ros/noetic/setup.bash}"
ROS_WORKSPACE_SETUP="${FIREBOT_ROS_WORKSPACE_SETUP:-}"
BRIDGE_ENV="${FIREBOT_BRIDGE_ENV:-/etc/firebot/bridge.env}"
SECRET_ENV="/etc/firebot/bridge-secret.env"

echo "== Firebot Bridge 安装 =="
echo "  INSTALL_DIR=$INSTALL_DIR"
echo "  BRIDGE_USER=$BRIDGE_USER"
echo "  ROS_SETUP=$ROS_SETUP"
echo "  ROS_WORKSPACE_SETUP=${ROS_WORKSPACE_SETUP:-<空>}"

# 1) 依赖校验
command -v python3 >/dev/null || { echo "ERROR: python3 未安装" >&2; exit 1; }
python3 -c "import paho.mqtt" 2>/dev/null || { echo "ERROR: paho-mqtt 未安装（pip3 install -r requirements.txt）" >&2; exit 1; }

# 2) ROS setup 校验（缺失仅告警：无 ROS 时为 MQTT-only，命令会回 rejected）
if [ -n "$ROS_SETUP" ] && [ -f "$ROS_SETUP" ]; then
  echo "  ROS setup 存在: $ROS_SETUP"
else
  echo "  WARN: ROS setup 不存在: $ROS_SETUP（将以 MQTT-only 模式运行）" >&2
fi

# 3) 安装/同步代码（不含 secret、不含 .git）
echo "  安装代码到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r "$SCRIPT_DIR/firebot_bridge" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/run_bridge.sh" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/config"
[ -f "$INSTALL_DIR/config/bridge.env" ] || cp "$SCRIPT_DIR/config/bridge.env.example" "$INSTALL_DIR/config/bridge.env"

# 4) 生成 systemd unit（固定安装位置，不绑定开发人员 home）
UNIT_TMP="$(mktemp)"
sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@BRIDGE_USER@|$BRIDGE_USER|g" \
  -e "s|@ROS_SETUP@|$ROS_SETUP|g" \
  -e "s|@ROS_WORKSPACE_SETUP@|$ROS_WORKSPACE_SETUP|g" \
  "$SCRIPT_DIR/systemd/firebot-bridge.service.template" > "$UNIT_TMP"

echo "  安装 systemd unit ..."
sudo cp "$UNIT_TMP" /etc/systemd/system/firebot-bridge.service
rm -f "$UNIT_TMP"
sudo systemctl daemon-reload

# 5) 配置与 secret 提示（不自动写密码）
echo ""
if [ ! -f "$SECRET_ENV" ]; then
  echo "  未找到 $SECRET_ENV —— 请手动创建（root:root 600）："
  echo "    sudo install -m 600 /dev/null $SECRET_ENV"
  echo "    echo 'FIREBOT_MQTT_PASSWORD=<车辆MQTT密码>' | sudo tee $SECRET_ENV >/dev/null"
  echo "    sudo chmod 600 $SECRET_ENV"
else
  echo "  secret 文件已存在: $SECRET_ENV"
fi

echo ""
echo "  完成。下一步："
echo "    1. 编辑 $BRIDGE_ENV（或 $INSTALL_DIR/config/bridge.env）确认 SITE/MAP/频率/STUB"
echo "    2. 确认 secret 后：sudo systemctl enable --now firebot-bridge"
echo "    3. 运行 verify.sh 验收"
