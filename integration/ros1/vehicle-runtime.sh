#!/usr/bin/env bash
# vehicle-runtime.sh — 车端运行模块化入口（一个命令只做一件事，禁止偷偷混合启动）
#
# 用法:
#   ./vehicle-runtime.sh status          只读查看，不启动任何东西
#   ./vehicle-runtime.sh ros-base        只启动真实底盘 bringup（不启动导航/控制）
#   ./vehicle-runtime.sh navigation      只启动 navigation + pose_navi_server
#   ./vehicle-runtime.sh control-start   只启动 firebot_control_adapter
#   ./vehicle-runtime.sh control-stop    只停 firebot_control_adapter
#   ./vehicle-runtime.sh verify          只读验收（等价 status）
#   ./vehicle-runtime.sh real-precheck   只读：硬件/control_mode/odom/cmd_vel 拓扑
#
# 边界：REAL 模式绝不启动 test_battery_pub、绝不注入 TEST_INJECTED、绝不伪造传感器值。
#       Bridge 由 systemd 独立维护，本脚本不启动 Bridge。
set -euo pipefail

WS="${FIREBOT_ROS_WORKSPACE:-/home/tl/firerobot_ws}"
LOG_DIR="$WS/logs/r1"
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"

source /opt/ros/noetic/setup.bash 2>/dev/null || true
source "$WS/devel/setup.bash" 2>/dev/null || true

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
  [ -e /dev/agv ] || compgen -G "/dev/ttyUSB*" >/dev/null 2>&1
}
control_mode_value() {
  timeout 3 rostopic echo -n1 /robot_status 2>/dev/null | grep -oP '(?<=control_mode: )\S+' | head -1 || true
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
  if [ "$(topic_pub_count /robot_status)" -gt 0 ]; then return; fi
  nohup roslaunch smartcar_description bringup_dual_lidar.launch > "$LOG_DIR/bringup.log" 2>&1 &
}

start_real_navigation() {
  if [ "$(topic_pub_count /move_base/status)" -gt 0 ]; then return; fi
  nohup roslaunch navigation navigation.launch > "$LOG_DIR/navigation.log" 2>&1 &
}

start_pose_navi_server() {
  if [ "$(topic_sub_count /waterplus/navi_pose)" -gt 0 ]; then return; fi
  rosnode cleanup >/dev/null 2>&1 || true
  nohup roslaunch waterplus_map_tools pose_navi_server.launch > "$LOG_DIR/pose_navi_server.log" 2>&1 &
}

start_adapter() {
  if adapter_ok; then
    echo "firebot_control_adapter 已在运行"
    return
  fi
  nohup rosrun firebot_control firebot_control_adapter.py > "$LOG_DIR/control_adapter.log" 2>&1 &
  sleep 2
}

stop_adapter() {
  if adapter_ok; then
    pkill -f "firebot_control_adapter" || true
    echo "firebot_control_adapter 已停止"
  else
    echo "firebot_control_adapter 未运行"
  fi
}

# ---------------- 只读检查 ----------------
do_status() {
  local cmode
  cmode=$(control_mode_value)
  echo "========== 车端运行状态（只读） =========="
  echo "ROS主节点：$(rosmaster_ok && echo 运行中 || echo 未运行)"
  echo "底盘驱动(/robot_status)：$( [ "$(topic_pub_count /robot_status)" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "move_base：$( [ "$(topic_pub_count /move_base/status)" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "pose_navi_server：$( [ "$(topic_sub_count /waterplus/navi_pose)" -gt 0 ] && echo 运行中 || echo 未运行)"
  echo "firebot_control_adapter：$(adapter_ok && echo 运行中 || echo 未运行)"
  echo "test_battery_pub：$(battery_ok && echo 运行中 || echo 未运行)"
  echo "底盘控制模式：$( [ "$cmode" = "3" ] && echo "ROS模式(3)" || echo "非ROS(未知)" )"
  echo "真实硬件：$(hardware_available && echo 已检测 || echo 未检测)"
}

do_real_precheck() {
  # 只读，不启动任何组件；逐项输出 PASS/FAIL
  local hw="FAIL"; hardware_available && hw="PASS"
  local cmode; cmode=$(control_mode_value); local cm="FAIL"; [ "$cmode" = "3" ] && cm="PASS"
  local odom="FAIL"; [ "$(topic_pub_count /odom)" -gt 0 ] && odom="PASS"
  local cmdvel_sub="FAIL"; [ "$(topic_sub_count /cmd_vel)" -gt 0 ] && cmdvel_sub="PASS"
  local amcl="FAIL"; [ "$(topic_pub_count /amcl_pose)" -gt 0 ] && amcl="PASS"

  echo "========== 实车预检（只读） =========="
  echo "真实硬件：$hw"
  echo "底盘 control_mode==3：$cm"
  echo "真实 /odom 有发布者：$odom"
  echo "/cmd_vel 有订阅者：$cmdvel_sub"
  echo "AMCL /amcl_pose 有发布者：$amcl"
  if [ "$hw" = "PASS" ] && [ "$cm" = "PASS" ] && [ "$odom" = "PASS" ] && [ "$cmdvel_sub" = "PASS" ]; then
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
