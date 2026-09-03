#!/usr/bin/env bash
# r1_vehicle.sh — Firebot R1 车端统一入口（终端中文化）
# 用法: ./r1_vehicle.sh sim | real | status | watch
#   sim    : 幂等启动/复用通信链组件（Bridge/ROS adapter/control adapter/TEST battery）并做 Gate 检查
#   real   : 复用真实车辆 bringup + navigation 栈，fail-closed motion gate（真实运动就绪判定）
#   status : 只读检查，不启动任何东西
#   watch  : 只显示关键事件（中文提示），Ctrl+C 仅退出 viewer
# 说明：Gate 判断逻辑不变，仅面向终端的状态文字中文化。
set -euo pipefail

WS="${FIREBOT_ROS_WORKSPACE:-/home/tl/firerobot_ws}"
LOG_DIR="$WS/logs/r1"
BRIDGE_DIR="${FIREBOT_BRIDGE_DIR:-/opt/firebot/vehicle-bridge}"
BRIDGE_ENV="${FIREBOT_BRIDGE_ENV:-/etc/firebot/bridge.env}"
# 现场 sudo 密码只经环境变量注入，绝不写死进仓库（原现场值已脱敏）
SUDO_PW="${FIREBOT_SUDO_PW:-}"
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"

# ROS 环境（必须在任何 ros* 命令之前 source）
source /opt/ros/noetic/setup.bash 2>/dev/null || true
source "$WS/devel/setup.bash" 2>/dev/null || true

CMD="${1:-sim}"

# ---------------- 工具函数 ----------------
proc_alive() { pgrep -f "$1" >/dev/null 2>&1; }
node_alive() { rosnode list 2>/dev/null | grep -qE "^/$1(\s|$)"; }
sudo_cat()  { echo "$SUDO_PW" | sudo -S cat "$1" 2>/dev/null; }

# status.json 实际位置（优先 systemd /run，fallback /tmp）
detect_status_file() {
  for f in /run/firebot-bridge/status.json /tmp/firebot-bridge-status.json; do
    [ -f "$f" ] && { STATUS_FILE="$f"; return; }
  done
  STATUS_FILE=""
}
STATUS_FILE=""

read_status_json() {
  detect_status_file
  [ -z "$STATUS_FILE" ] && { echo "{}"; return; }
  if [ -r "$STATUS_FILE" ]; then cat "$STATUS_FILE"; else sudo_cat "$STATUS_FILE"; fi
}

# 读 status.json 布尔字段 → "true"/"false"
status_bool() {
  read_status_json | python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if d.get('$1') is True else 'false')" 2>/dev/null || echo "false"
}

# 读 status.json 列表字段 → JSON 数组字符串
status_list() {
  read_status_json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('$1', [])))" 2>/dev/null || echo "[]"
}

# 读一次 battery 数值
battery_value() {
  timeout 3 rostopic echo -n1 /firebot_bridge/battery 2>/dev/null | grep -oP '(?<=data: )\S+' | head -1 || true
}

# 电量值格式化 → 一位小数（82.4000015 → 82.4）
fmt_battery() {
  python3 -c "import sys; print(f'{float(sys.argv[1]):.1f}')" "$1" 2>/dev/null || echo "$1"
}

# 话题 publisher/subscriber 计数（用于 fail-closed readiness 判定）
# 末尾 || true 吸收管道非零退出码，避免 set -e 在赋值时静默退出
topic_pub_count() {
  rostopic info "$1" 2>/dev/null | awk '/^Publishers:/{f=1;next} /^Subscribers:/{f=0} f&&/^\s*\*/{c++} END{print c+0}' || true
}
topic_sub_count() {
  rostopic info "$1" 2>/dev/null | awk '/^Subscribers:/{f=1;next} /^Publishers:/{f=0} f&&/^\s*\*/{c++} END{print c+0}' || true
}

# 底盘硬件是否在位（/dev/agv 或 /dev/ttyUSB*）—— 决定是否可能真实运动
hardware_available() {
  [ -e /dev/agv ] || compgen -G "/dev/ttyUSB*" >/dev/null 2>&1
}

# 读底盘控制模式（/robot_status 的 control_mode 字段，3=ROS模式）
control_mode_value() {
  timeout 3 rostopic echo -n1 /robot_status 2>/dev/null | grep -oP '(?<=control_mode: )\S+' | head -1 || true
}

# 失败阶段 → 中文（仅显示层）
stage_cn() {
  case "$1" in
    ROS_MASTER)         echo "ROS 主节点" ;;
    MQTT_CONNECT)       echo "服务器连接" ;;
    ROS_ADAPTER)        echo "ROS 桥接" ;;
    CONTROL_ADAPTER)    echo "巡检控制适配器" ;;
    POSE_NAVI_SERVER)   echo "导航接收节点" ;;
    MOVE_BASE)          echo "导航系统 move_base" ;;
    CMD_VEL_DRIVER)     echo "底盘控制" ;;
    LOCALIZATION)       echo "定位系统" ;;
    SUPPORTED_COMMANDS) echo "支持的指令" ;;
    CONTROL_MODE)       echo "底盘控制模式" ;;
    REAL_HARDWARE)      echo "真实硬件" ;;
    *)                  echo "$1" ;;
  esac
}

# ---------------- 组件状态 ----------------
rosmaster_ok()     { proc_alive "rosmaster --core"; }
bridge_parent_ok() { proc_alive "firebot_bridge.main"; }
adapter_ok()       { proc_alive "firebot_control_adapter"; }
battery_ok()       { proc_alive "test_battery_pub"; }
bridge_systemd_ok(){ systemctl is-active --quiet firebot-bridge 2>/dev/null; }
bridge_parent_pid(){ pgrep -f "firebot_bridge.main" 2>/dev/null | head -1; }

# 真实运动栈就绪判定（fail-closed，基于真实话题 pub/sub，不模拟）
pose_navi_server_ok() { [ "$(topic_sub_count /waterplus/navi_pose)" -gt 0 ]; }
move_base_ok()        { [ "$(topic_pub_count /move_base/status)" -gt 0 ]; }
cmd_vel_driver_ok()   {
  rostopic info /cmd_vel 2>/dev/null | awk '/Subscribers:/{f=1;next} /Publishers:/{f=0} f&&/^\s*\*/{print}' \
    | grep -qiE "igk_robot|serial_485|485|agv"
}
localization_ok() {
  # 定位链存在且当前不失效：/amcl_pose 有 publisher 且最近有真实数据
  [ "$(topic_pub_count /amcl_pose)" -gt 0 ] && \
    timeout 3 rostopic echo -n1 /amcl_pose >/dev/null 2>&1
}

# ---------------- 启动组件（幂等） ----------------
start_rosmaster() {
  if rosmaster_ok; then return; fi
  nohup roscore > "$LOG_DIR/roscore.log" 2>&1 &
  for _ in $(seq 1 15); do
    rosnode list >/dev/null 2>&1 && break
    sleep 1
  done
}

start_adapter() {
  if adapter_ok; then return; fi
  nohup rosrun firebot_control firebot_control_adapter.py > "$LOG_DIR/control_adapter.log" 2>&1 &
  sleep 2
}

start_battery() {
  if battery_ok; then return; fi
  {
    echo "BATTERY_SOURCE=TEST_INJECTED"
    echo "REAL_BATTERY_VERIFIED=NO"
  } > "$LOG_DIR/test_battery.log"
  nohup rosrun firebot_control test_battery_pub.py _value:=82.4 >> "$LOG_DIR/test_battery.log" 2>&1 &
  sleep 1
}

start_bridge() {
  # 1) systemd 优先
  if bridge_systemd_ok; then return; fi
  # 2) 已有非 systemd parent → 复用，绝不启动第二个
  if bridge_parent_ok; then return; fi
  # 3) 都没有 → 启动（需 sudo 读 secret）
  echo "$SUDO_PW" | sudo -S bash -c "
    export FIREBOT_FIELD_TRACE=true
    export ROS_MASTER_URI='$ROS_MASTER_URI'
    export FIREBOT_ROS_SETUP=/opt/ros/noetic/setup.bash
    export FIREBOT_ROS_WORKSPACE_SETUP='$WS/devel/setup.bash'
    cd '$BRIDGE_DIR'
    mkdir -p logs
    nohup bash run_bridge.sh '$BRIDGE_ENV' > logs/bridge.log 2>&1 &
  " 2>/dev/null
}

# ---------------- real：真实车辆启动栈（复用现有正式 launch，不手工拼节点） ----------------
start_real_bringup() {
  # 底盘驱动已在运行（/robot_status 有 publisher）→ 复用
  if [ "$(topic_pub_count /robot_status)" -gt 0 ]; then return; fi
  nohup roslaunch smartcar_description bringup_dual_lidar.launch > "$LOG_DIR/bringup.log" 2>&1 &
}

start_real_navigation() {
  # move_base 已在运行 → 复用
  if [ "$(topic_pub_count /move_base/status)" -gt 0 ]; then return; fi
  nohup roslaunch navigation navigation.launch > "$LOG_DIR/navigation.log" 2>&1 &
}

start_pose_navi_server() {
  # pose_navi_server 已订阅 /waterplus/navi_pose → 复用
  if [ "$(topic_sub_count /waterplus/navi_pose)" -gt 0 ]; then return; fi
  # 清理 master 上残留的僵尸节点注册，避免同名冲突（不 kill 进程，只清注册）
  rosnode cleanup >/dev/null 2>&1 || true
  nohup roslaunch waterplus_map_tools pose_navi_server.launch > "$LOG_DIR/pose_navi_server.log" 2>&1 &
}

# ---------------- MQTT 等待（最多 120 秒） ----------------
wait_mqtt() {
  # 每 2 秒读一次 status.json，最多 120 秒；不重启 Bridge、不动网络
  for _ in $(seq 1 60); do
    if [ "$(status_bool mqtt_connected)" = "true" ]; then return 0; fi
    sleep 2
  done
  return 1
}

# ---------------- sim：通信链检查（中文输出） ----------------
do_sim() {
  mkdir -p "$LOG_DIR"
  start_rosmaster
  start_adapter
  start_battery
  start_bridge

  # ROS gate（立即检查，不依赖 MQTT）
  sleep 2
  local cmd_sub fb_pub batt
  cmd_sub=$(rosnode info /firebot_bridge/command 2>/dev/null | grep -q 'firebot_control_adapter' && echo PASS || echo FAIL)
  fb_pub=$(rosnode info /firebot_bridge/command_feedback 2>/dev/null | grep -q 'firebot_control_adapter' && echo PASS || echo FAIL)
  batt=$(battery_value)
  [ -n "$batt" ] && batt=$(fmt_battery "$batt")

  # 等 MQTT（最多 120 秒，每 2 秒读一次 status.json；不重启 Bridge/不动网络）
  if ! wait_mqtt; then
    echo "========== 车端通信检查 =========="
    echo ""
    echo "ROS主节点：$(rosmaster_ok && echo 通过 || echo 失败)"
    echo "服务器通信：失败"
    echo "ROS桥接：$( [ "$(status_bool ros_adapter_ready)" = "true" ] && echo 通过 || echo 失败)"
    echo "巡检控制适配器：$( [ "$cmd_sub" = "PASS" ] && echo 通过 || echo 失败)"
    echo ""
    echo "车端通信状态：未就绪"
    echo "失败原因：无法连接服务器 MQTT"
    echo "提示：Bridge 会自动重连，请稍后重新运行本命令"
    exit 1
  fi

  # 完整 gate：ROS 关键字段全部就绪才 PASS
  local fail_fields=""
  for k in ros_master_available ros_node_ready ros_command_publisher_ready ros_feedback_ready ros_adapter_ready battery_provider_seen; do
    if [ "$(status_bool "$k")" != "true" ]; then fail_fields="$fail_fields $k"; fi
  done
  if [ -n "$fail_fields" ]; then
    echo "========== 车端通信检查 =========="
    echo ""
    echo "ROS主节点：$(rosmaster_ok && echo 通过 || echo 失败)"
    echo "服务器通信：通过"
    echo "ROS桥接：$( [ "$(status_bool ros_adapter_ready)" = "true" ] && echo 通过 || echo 失败)"
    echo "巡检控制适配器：$( [ "$cmd_sub" = "PASS" ] && [ "$fb_pub" = "PASS" ] && echo 通过 || echo 失败)"
    echo ""
    echo "车端通信状态：未就绪"
    echo "失败原因：ROS 通信链路未完全就绪"
    exit 1
  fi

  # 成功最终输出
  echo "========== 车端通信检查 =========="
  echo ""
  echo "ROS主节点：通过"
  echo "服务器通信：通过"
  echo "ROS桥接：通过"
  echo "巡检控制适配器：通过"
  echo ""
  echo "巡检指令链路：通过"
  echo "巡检启动接口：/waterplus/navi_pose"
  echo ""
  echo "电量数据源：模拟测试数据"
  echo "当前电量：${batt:-N/A}%"
  echo ""
  echo "真实硬件：未连接"
  echo "真实车辆运动：未验证"
  echo ""
  echo "车端通信状态：已就绪"
  echo "下一步：可以到服务器发送“开始巡检”指令"
}

# ---------------- real：真实运动栈 + motion gate ----------------
do_real() {
  echo "WARN: r1_vehicle.sh real 已弃用，请改用 ./vehicle-runtime.sh（模块化：ros-base/navigation/control-start/control-stop）" >&2
  mkdir -p "$LOG_DIR"

  # 硬件是否在位（缺底盘串口 = 备份机/无硬件）
  local hw="YES"
  hardware_available || hw="NO"

  start_rosmaster
  start_real_bringup
  start_real_navigation
  start_pose_navi_server
  start_adapter
  start_bridge

  # 等 move_base 起来（最多 60 秒，每 2 秒；加载地图/costmap 较慢）
  local mb_wait=60
  while [ "$(topic_pub_count /move_base/status)" -eq 0 ] && [ "$mb_wait" -gt 0 ]; do
    sleep 2
    mb_wait=$((mb_wait-2))
  done

  # 等 pose_navi_server 订阅 /waterplus/navi_pose（最多 15 秒）
  local pn_wait=15
  while [ "$(topic_sub_count /waterplus/navi_pose)" -eq 0 ] && [ "$pn_wait" -gt 0 ]; do
    sleep 1
    pn_wait=$((pn_wait-1))
  done

  # 等 MQTT（最多 120 秒）
  local mqtt_ok="false"
  wait_mqtt && mqtt_ok="true"

  motion_gate "$hw" "$mqtt_ok"
}

# ---------------- REAL MOTION GATE（判断条件不变，仅输出中文化） ----------------
motion_gate() {
  local hw="$1" mqtt_ok="$2"

  local ros_master="FAIL";  rosmaster_ok && ros_master="PASS"
  local bridge_mqtt="FAIL"; [ "$mqtt_ok" = "true" ] && bridge_mqtt="PASS"
  local ros_adapter="FAIL"; [ "$(status_bool ros_adapter_ready)" = "true" ] && ros_adapter="PASS"
  local control_adapter="FAIL"; adapter_ok && control_adapter="PASS"

  local pose_navi="FAIL";  pose_navi_server_ok && pose_navi="PASS"
  local move_base="FAIL";  move_base_ok && move_base="PASS"
  local cmdvel="FAIL";     cmd_vel_driver_ok && cmdvel="PASS"
  local loc="FAIL";        localization_ok && loc="PASS"

  local supported; supported=$(status_list supported_commands)
  local has_patrol="no"; echo "$supported" | grep -q '"patrol"' && has_patrol="yes"
  local cmode; cmode=$(control_mode_value)
  local batt; batt=$(battery_value); [ -n "$batt" ] && batt=$(fmt_battery "$batt")

  # 综合判定（fail-closed：任一缺失即 FAIL，绝不 PASS）
  # REAL 模式必须硬要求底盘 control_mode == 3（ROS 控制模式），不得只展示不强制。
  local ready="FAIL"
  if [ "$ros_master" = "PASS" ] && [ "$bridge_mqtt" = "PASS" ] && \
     [ "$ros_adapter" = "PASS" ] && [ "$control_adapter" = "PASS" ] && \
     [ "$pose_navi" = "PASS" ] && [ "$move_base" = "PASS" ] && \
     [ "$cmdvel" = "PASS" ] && [ "$loc" = "PASS" ] && \
     [ "$cmode" = "3" ] && [ "$has_patrol" = "yes" ] && [ "$hw" = "YES" ]; then
    ready="PASS"
  fi

  local stage=""
  [ "$ros_master" != "PASS" ]        && stage="ROS_MASTER"
  [ -z "$stage" ] && [ "$bridge_mqtt" != "PASS" ] && stage="MQTT_CONNECT"
  [ -z "$stage" ] && [ "$ros_adapter" != "PASS" ] && stage="ROS_ADAPTER"
  [ -z "$stage" ] && [ "$control_adapter" != "PASS" ] && stage="CONTROL_ADAPTER"
  [ -z "$stage" ] && [ "$pose_navi" != "PASS" ] && stage="POSE_NAVI_SERVER"
  [ -z "$stage" ] && [ "$move_base" != "PASS" ] && stage="MOVE_BASE"
  [ -z "$stage" ] && [ "$cmdvel" != "PASS" ] && stage="CMD_VEL_DRIVER"
  [ -z "$stage" ] && [ "$loc" != "PASS" ] && stage="LOCALIZATION"
  [ -z "$stage" ] && [ "$cmode" != "3" ] && stage="CONTROL_MODE"
  [ -z "$stage" ] && [ "$has_patrol" != "yes" ] && stage="SUPPORTED_COMMANDS"
  [ -z "$stage" ] && [ "$hw" = "NO" ] && stage="REAL_HARDWARE"

  # ---- 中文输出 ----
  echo "========== 实车运行检查 =========="
  echo ""
  echo "ROS主节点：$( [ "$ros_master" = "PASS" ] && echo 通过 || echo 失败)"
  echo "服务器通信：$( [ "$bridge_mqtt" = "PASS" ] && echo 通过 || echo 失败)"
  echo "ROS桥接：$( [ "$ros_adapter" = "PASS" ] && echo 通过 || echo 失败)"
  echo "巡检控制适配器：$( [ "$control_adapter" = "PASS" ] && echo 通过 || echo 失败)"
  echo ""
  echo "导航接收节点：$( [ "$pose_navi" = "PASS" ] && echo 通过 || echo 失败)"
  echo "导航系统 move_base：$( [ "$move_base" = "PASS" ] && echo 通过 || echo 失败)"
  echo "底盘控制：$( [ "$cmdvel" = "PASS" ] && echo 通过 || echo 失败)"
  echo "定位系统：$( [ "$loc" = "PASS" ] && echo 通过 || echo 失败)"
  echo "底盘控制模式：$( [ "$cmode" = "3" ] && echo ROS模式 || echo 未知)"
  echo ""
  echo "巡检启动接口：/waterplus/navi_pose"
  echo ""
  echo "电量数据源：真实 provider（未验证）"
  echo "当前电量：${batt:-N/A}%"
  echo ""
  if [ "$hw" = "YES" ]; then
    echo "真实硬件：已连接"
  else
    echo "真实硬件：未检测到"
  fi

  if [ "$ready" = "PASS" ]; then
    echo "车辆运行条件：全部满足"
    echo ""
    echo "车端状态：已就绪"
    echo "下一步：可以到服务器发送“开始巡检”指令"
    exit 0
  else
    echo "车辆运行条件：未满足"
    echo "车端状态：未就绪"
    echo ""
    echo "失败位置：$(stage_cn "$stage")"
    if [ "$hw" = "NO" ]; then
      echo "说明：当前设备没有连接实车硬件，不能进行真实运动验证"
    else
      echo "说明：请检查「$(stage_cn "$stage")」相关组件是否正常启动"
    fi
    exit 1
  fi
}

# ---------------- status：车端当前状态（只读，中文） ----------------
do_status() {
  local batt cmode
  batt=$(battery_value); [ -n "$batt" ] && batt=$(fmt_battery "$batt")
  cmode=$(control_mode_value)

  echo "========== 车端当前状态 =========="
  echo ""
  echo "ROS主节点：$(rosmaster_ok && echo 运行中 || echo 未运行)"
  if bridge_systemd_ok || bridge_parent_ok; then
    echo "Bridge：运行中"
  else
    echo "Bridge：未运行"
  fi
  echo "ROS桥接：$( [ "$(status_bool ros_adapter_ready)" = "true" ] && echo 运行中 || echo 未运行)"
  echo "巡检控制适配器：$(adapter_ok && echo 运行中 || echo 未运行)"
  echo "测试电量节点：$(battery_ok && echo 运行中 || echo 未运行)"
  echo ""
  echo "服务器连接：$( [ "$(status_bool mqtt_connected)" = "true" ] && echo 已连接 || echo 未连接)"
  echo "当前启动编号：$(read_status_json | python3 -c "import json,sys; print(json.load(sys.stdin).get('boot_id','')[:8])" 2>/dev/null)"
  echo "支持的指令：$(echo "$(status_list supported_commands)" | grep -q '"patrol"' && echo 开始巡检 || echo 无)"
  echo ""
  echo "当前电量：${batt:-N/A}%"
  echo "电量来源：模拟测试数据"
  echo ""
  echo "---------- 实车运动环境 ----------"
  echo ""
  echo "真实底盘：$(hardware_available && echo 已检测 || echo 未检测)"
  echo "底盘控制模式：$( [ "$cmode" = "3" ] && echo ROS模式 || echo 未知)"
  echo "导航接收节点：$(pose_navi_server_ok && echo 运行中 || echo 未运行)"
  echo "move_base：$(move_base_ok && echo 运行中 || echo 未运行)"
  echo "底盘速度接口：$(cmd_vel_driver_ok && echo 正常 || echo 未连接)"
  echo "定位系统：$(localization_ok && echo 正常 || echo 未就绪)"
}

# ---------------- watch：关键事件中文 viewer ----------------
do_watch() {
  detect_status_file
  local events_file="${FIREBOT_EVENTS_DIR:-$BRIDGE_DIR/logs}/events.jsonl"
  echo "Firebot R1 车端运行事件（Ctrl+C 仅退出 viewer，不影响 Bridge/ROS）"
  # 单一事实源：历史回放 + 实时跟随都来自 events.jsonl（tail -F 跨轮转跟随，天然无重复）
  tail -n 15 -F "$events_file" 2>/dev/null \
    | python3 -u "$BRIDGE_DIR/tools/field_console.py" --jsonl \
        --status-file "${STATUS_FILE:-/run/firebot-bridge/status.json}" --lang zh "$@"
}

do_trace() {
  local events_dir="${FIREBOT_EVENTS_DIR:-$BRIDGE_DIR/logs}"
  python3 "$BRIDGE_DIR/tools/trace_timeline.py" "$events_dir" "${1:-latest}" --lang zh
}

# ---------------- 入口 ----------------
case "$CMD" in
  sim)    do_sim ;;
  real)   do_real ;;
  status) do_status ;;
  watch)  do_watch ;;
  trace)  do_trace "${2:-latest}" ;;
  *) echo "用法: $0 sim|real|status|watch|trace <command_id|latest>" >&2; exit 1 ;;
esac
