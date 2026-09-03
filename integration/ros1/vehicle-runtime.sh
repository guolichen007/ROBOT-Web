#!/usr/bin/env bash
# vehicle-runtime.sh — 车端运行模块化入口（一个命令只做一件事，禁止偷偷混合启动）
#
# 用法:
#   ./vehicle-runtime.sh status          只读查看，不启动任何东西
#   ./vehicle-runtime.sh ros-base        只启动真实底盘 bringup（不启动导航/控制）
#   ./vehicle-runtime.sh navigation      只启动 navigation + pose_navi_server
#   ./vehicle-runtime.sh control-start   只启动 firebot_control_adapter（systemd 单路径）
#   ./vehicle-runtime.sh control-stop    只停 firebot_control_adapter
#   ./vehicle-runtime.sh real-precheck   只读：硬件/control_mode/odom/cmd_vel/AMCL
#
# 边界：REAL 模式绝不启动 test_battery_pub、绝不注入 TEST_INJECTED、绝不伪造传感器值。
#       Bridge 由 systemd 独立维护，本脚本不启动 Bridge。
#       车型参数一律来自 /etc/firebot/runtime.env（由 firebotctl 从 Fleet Profile 生成），不硬编码。
set -euo pipefail

# ---- runtime 配置：Fleet Profile 生成，删除车型硬编码 ----
if [ -f /etc/firebot/runtime.env ]; then
  # shellcheck disable=SC1091
  . /etc/firebot/runtime.env
fi
ROS_DISTRO="${ROS_DISTRO:-noetic}"
ROS_WORKSPACE="${ROS_WORKSPACE:-/home/tl/firerobot_ws}"
BASE_DEVICE="${BASE_DEVICE:-/dev/agv}"
ROBOT_STATUS_TOPIC="${ROBOT_STATUS_TOPIC:-/robot_status}"
ODOM_TOPIC="${ODOM_TOPIC:-/odom}"
CMD_VEL_TOPIC="${CMD_VEL_TOPIC:-/cmd_vel}"
AMCL_TOPIC="${AMCL_TOPIC:-/amcl_pose}"
POSE_TOPIC="${POSE_TOPIC:-/waterplus/navi_pose}"
EXPECTED_CONTROL_MODE="${EXPECTED_CONTROL_MODE:-3}"
BASE_LAUNCH_PACKAGE="${BASE_LAUNCH_PACKAGE:-smartcar_description}"
BASE_LAUNCH_FILE="${BASE_LAUNCH_FILE:-bringup_dual_lidar.launch}"
NAV_LAUNCH_PACKAGE="${NAV_LAUNCH_PACKAGE:-navigation}"
NAV_LAUNCH_FILE="${NAV_LAUNCH_FILE:-navigation.launch}"
POSE_SERVER_PACKAGE="${POSE_SERVER_PACKAGE:-waterplus_map_tools}"
POSE_SERVER_LAUNCH="${POSE_SERVER_LAUNCH:-pose_navi_server.launch}"
CONTROL_ADAPTER="${CONTROL_ADAPTER:-firebot_control_adapter.py}"

LOG_DIR="$ROS_WORKSPACE/logs/r1"
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"

source "/opt/ros/$ROS_DISTRO/setup.bash" 2>/dev/null || true
source "$ROS_WORKSPACE/devel/setup.bash" 2>/dev/null || true

CMD="${1:-status}"

# ---------------- 工具函数 ----------------
proc_alive() { pgrep -f "$1" >/dev/null 2>&1; }
rosmaster_ok() { proc_alive "rosmaster --core"; }
adapter_ok()  { proc_alive "firebot_control_adapter"; }
battery_ok()  { proc_alive "test_battery_pub"; }

topic_pub_count() {
  rostopic info "$1" 2>/dev/null | awk '/^Publishers:/{f=1;next} /^Subscribers:/{f=0} f&&/^\s*\*/{c++} END{print c+0}' || true
}
topic_sub_count() {
  rostopic info "$1" 2>/dev/null | awk '/^Subscribers:/{f=1;next} /^Publishers:/{f=0} f&&/^\s*\*/{c++} END{print c+0}' || true
}
hardware_available() {
  # 按 Profile BASE_DEVICE 判定，不允许任意 ttyUSB 即 PASS
  [ -e "$BASE_DEVICE" ]
}
control_mode_value() {
  timeout 3 rostopic echo -n1 "$ROBOT_STATUS_TOPIC" 2>/dev/null | grep -oP '(?<=control_mode: )\S+' | head -1 || true
}
runtime_shas_match() {
  local br cr
  br="$(cat /opt/firebot/vehicle-bridge/APPROVED_RUNTIME.txt 2>/dev/null || echo MISSING)"
  cr="$(cat "$ROS_WORKSPACE/src/firebot_control/APPROVED_RUNTIME.txt" 2>/dev/null || echo MISSING)"
  [ "$br" != "MISSING" ] && [ "$br" = "$cr" ]
}

# ---------------- 启动（每个函数只启动单一职责组件，幂等） ----------------
start_rosmaster() {
  if rosmaster_ok; then return; fi
  nohup roscore > "$LOG_DIR/roscore.log" 2>&1 &
  for _ in $(seq 1 15); do
    rosnode list >/dev/null 2>&1 && break
    sleep 1
  done
}

start_real_bringup() {
  if [ "$(topic_pub_count "$ROBOT_STATUS_TOPIC")" -gt 0 ]; then return; fi
  nohup roslaunch "$BASE_LAUNCH_PACKAGE" "$BASE_LAUNCH_FILE" > "$LOG_DIR/bringup.log" 2>&1 &
}

start_real_navigation() {
  if [ "$(topic_pub_count /move_base/status)" -gt 0 ]; then return; fi
  nohup roslaunch "$NAV_LAUNCH_PACKAGE" "$NAV_LAUNCH_FILE" > "$LOG_DIR/navigation.log" 2>&1 &
}

start_pose_navi_server() {
  if [ "$(topic_sub_count "$POSE_TOPIC")" -gt 0 ]; then return; fi
  rosnode cleanup >/dev/null 2>&1 || true
  nohup roslaunch "$POSE_SERVER_PACKAGE" "$POSE_SERVER_LAUNCH" > "$LOG_DIR/pose_navi_server.log" 2>&1 &
}

control_gate() {
  # STOP-only 至少：SHA 一致 + ROS master + control_mode==3 + /cmd_vel 真实订阅者。
  # patrol 额外：AMCL fresh + move_base + pose_navi + map/location。
  local cmd cmode has_patrol="no"
  cmode="$(control_mode_value)"
  runtime_shas_match || { echo "ERROR: Bridge/Control APPROVED_RUNTIME SHA 不一致" >&2; return 1; }
  rosmaster_ok || { echo "ERROR: ROS master 未就绪" >&2; return 1; }
  [ "$cmode" = "$EXPECTED_CONTROL_MODE" ] || { echo "ERROR: control_mode=$cmode != $EXPECTED_CONTROL_MODE" >&2; return 1; }
  [ "$(topic_sub_count "$CMD_VEL_TOPIC")" -gt 0 ] || { echo "ERROR: $CMD_VEL_TOPIC 无订阅者" >&2; return 1; }
  cmd="$(grep -E '^FIREBOT_SUPPORTED_COMMANDS=' /etc/firebot/bridge.env 2>/dev/null | cut -d= -f2- || true)"
  echo "$cmd" | grep -q patrol && has_patrol="yes"
  if [ "$has_patrol" = "yes" ]; then
    [ "$(topic_pub_count "$AMCL_TOPIC")" -gt 0 ] || { echo "ERROR: $AMCL_TOPIC 无发布者（patrol 需要定位）" >&2; return 1; }
    [ "$(topic_pub_count /move_base/status)" -gt 0 ] || { echo "ERROR: move_base 未就绪（patrol 需要）" >&2; return 1; }
    [ "$(topic_sub_count "$POSE_TOPIC")" -gt 0 ] || { echo "ERROR: $POSE_TOPIC 无订阅者（patrol 需要）" >&2; return 1; }
  fi
  return 0
}

start_adapter() {
  if systemctl is-active --quiet firebot-control 2>/dev/null; then
    echo "firebot-control 已在运行（systemd）"
    return
  fi
  control_gate || { echo "CONTROL_START=REJECTED" >&2; return 1; }
  systemctl start firebot-control
  echo "CONTROL_START=PASS"
}

stop_adapter() {
  if systemctl is-active --quiet firebot-control 2>/dev/null; then
    systemctl stop firebot-control
    echo "firebot-control 已停止"
  else
    echo "firebot-control 未运行"
  fi
}

# ---------------- 只读检查 ----------------
do_status() {
  local cmode
  cmode=$(control_mode_value)
  echo "========== 车端运行状态（只读） =========="
  echo "ROS主节点：$(rosmaster_ok && echo 运行中 || echo 未运行)"
  echo "底盘驱动($ROBOT_STATUS_TOPIC)：$( [ "$(topic_pub_count "$ROBOT_STATUS_TOPIC")" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "move_base：$( [ "$(topic_pub_count /move_base/status)" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "pose_navi_server：$( [ "$(topic_sub_count "$POSE_TOPIC")" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "firebot-control：$(systemctl is-active firebot-control 2>/dev/null || echo 未运行)"
  echo "test_battery_pub：$(battery_ok && echo 运行中 || echo 未运行)"
  echo "底盘控制模式：$( [ "$cmode" = "$EXPECTED_CONTROL_MODE" ] && echo "ROS模式($EXPECTED_CONTROL_MODE)" || echo "非ROS(未知)" )"
  echo "真实硬件($BASE_DEVICE)：$(hardware_available && echo 已检测 || echo 未检测)"
}

do_real_precheck() {
  # 只读，不启动任何组件；逐项输出 PASS/FAIL；AMCL 必须进入最终 PASS 条件。
  local hw="FAIL"; hardware_available && hw="PASS"
  local cmode; cmode=$(control_mode_value); local cm="FAIL"; [ "$cmode" = "$EXPECTED_CONTROL_MODE" ] && cm="PASS"
  local odom="FAIL"; [ "$(topic_pub_count "$ODOM_TOPIC")" -gt 0 ] && odom="PASS"
  local cmdvel_sub="FAIL"; [ "$(topic_sub_count "$CMD_VEL_TOPIC")" -gt 0 ] && cmdvel_sub="PASS"
  local amcl="FAIL"; [ "$(topic_pub_count "$AMCL_TOPIC")" -gt 0 ] && amcl="PASS"

  echo "========== 实车预检（只读） =========="
  echo "真实硬件($BASE_DEVICE)：$hw"
  echo "底盘 control_mode==$EXPECTED_CONTROL_MODE：$cm"
  echo "真实 $ODOM_TOPIC 有发布者：$odom"
  echo "$CMD_VEL_TOPIC 有订阅者：$cmdvel_sub"
  echo "AMCL $AMCL_TOPIC 有发布者：$amcl"
  if [ "$hw" = "PASS" ] && [ "$cm" = "PASS" ] && [ "$odom" = "PASS" ] && [ "$cmdvel_sub" = "PASS" ] && [ "$amcl" = "PASS" ]; then
    echo "REAL_PRECHECK=PASS"
    return 0
  fi
  echo "REAL_PRECHECK=FAIL" >&2
  return 1
}

# ---------------- 入口 ----------------
case "$CMD" in
  status)        do_status ;;
  ros-base)      mkdir -p "$LOG_DIR"; start_rosmaster; start_real_bringup ;;
  navigation)    mkdir -p "$LOG_DIR"; start_real_navigation; start_pose_navi_server ;;
  control-start) mkdir -p "$LOG_DIR"; start_adapter ;;
  control-stop)  stop_adapter ;;
  verify)        do_status ;;
  real-precheck) do_real_precheck ;;
  *) echo "用法: $0 status|ros-base|navigation|control-start|control-stop|verify|real-precheck" >&2; exit 1 ;;
esac
