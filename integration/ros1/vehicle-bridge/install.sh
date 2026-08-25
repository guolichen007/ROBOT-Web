#!/usr/bin/env bash
# Firebot 车端 Bridge 安装脚本（GitHub 是唯一交付源；把代码装到固定目录并生成 systemd）
#
# 可配置（环境变量，均有默认值）：
#   FIREBOT_INSTALL_DIR        安装目录（默认 /opt/firebot/vehicle-bridge，需 sudo）
#   FIREBOT_BRIDGE_USER        systemd 运行用户（默认当前用户）
#   FIREBOT_ROS_SETUP          ROS setup.bash（默认 /opt/ros/noetic/setup.bash）
#   FIREBOT_ROS_WORKSPACE_SETUP 车端工作区 setup.bash（默认空）
#   FIREBOT_BRIDGE_ENV         车端 bridge.env 唯一真实位置（默认 /etc/firebot/bridge.env）
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
echo "  BRIDGE_ENV=$BRIDGE_ENV"

# 1) 依赖校验
command -v python3 >/dev/null || { echo "ERROR: python3 未安装" >&2; exit 1; }
python3 -c "import paho.mqtt" 2>/dev/null || { echo "ERROR: paho-mqtt 未安装（pip3 install -r requirements.txt）" >&2; exit 1; }

# 2) ROS setup 校验（缺失仅告警：无 ROS 时为 MQTT-only，命令会回 rejected）
if [ -n "$ROS_SETUP" ] && [ -f "$ROS_SETUP" ]; then
  echo "  ROS setup 存在: $ROS_SETUP"
else
  echo "  WARN: ROS setup 不存在: $ROS_SETUP（将以 MQTT-only 模式运行）" >&2
fi

# 3) 安装/同步代码到 /opt（需 sudo）；幂等：先清旧 firebot_bridge 避免残留已删除文件
echo "  安装代码到 $INSTALL_DIR ..."
sudo mkdir -p "$INSTALL_DIR" "$(dirname "$BRIDGE_ENV")"
sudo rm -rf "$INSTALL_DIR/firebot_bridge" "$INSTALL_DIR/tools"
sudo cp -r "$SCRIPT_DIR/firebot_bridge" "$INSTALL_DIR/"
sudo cp -r "$SCRIPT_DIR/tools" "$INSTALL_DIR/"
sudo cp "$SCRIPT_DIR/run_bridge.sh" "$INSTALL_DIR/"
sudo cp "$SCRIPT_DIR/watch-bridge.sh" "$INSTALL_DIR/"
sudo cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
sudo chmod 0755 "$INSTALL_DIR/watch-bridge.sh" "$INSTALL_DIR/run_bridge.sh"
sudo chown -R "$BRIDGE_USER":"$(id -gn "$BRIDGE_USER" 2>/dev/null || echo "$BRIDGE_USER")" "$INSTALL_DIR"

# 4) bridge.env 唯一真实位置（无密码）
if [ -f "$BRIDGE_ENV" ]; then
  echo "  bridge.env 已存在: $BRIDGE_ENV（保留，不覆盖）"
else
  sudo cp "$SCRIPT_DIR/config/bridge.env.example" "$BRIDGE_ENV"
  sudo chown "$BRIDGE_USER":"$(id -gn "$BRIDGE_USER" 2>/dev/null || echo "$BRIDGE_USER")" "$BRIDGE_ENV"
  echo "  已生成: $BRIDGE_ENV"
fi

# 5) 生成 systemd unit（固定安装位置，不绑定开发人员 home）
UNIT_TMP="$(mktemp)"
sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@BRIDGE_USER@|$BRIDGE_USER|g" \
  -e "s|@ROS_SETUP@|$ROS_SETUP|g" \
  -e "s|@ROS_WORKSPACE_SETUP@|$ROS_WORKSPACE_SETUP|g" \
  -e "s|@BRIDGE_ENV@|$BRIDGE_ENV|g" \
  "$SCRIPT_DIR/systemd/firebot-bridge.service.template" > "$UNIT_TMP"

echo "  安装 systemd unit ..."
sudo cp "$UNIT_TMP" /etc/systemd/system/firebot-bridge.service
rm -f "$UNIT_TMP"
sudo systemctl daemon-reload

# 6) 配置与 secret 提示（不自动写密码）
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
echo "    1. 编辑 $BRIDGE_ENV 确认 SITE/MAP/频率/STUB"
echo "    2. 确认 secret 后：sudo systemctl enable --now firebot-bridge"
echo "    3. 运行 verify.sh 验收"
