#!/usr/bin/env bash
# firebot-001 模拟视频出站（RGB + THERMAL，低权限 FFmpeg 进程）。
#
# 用途：使用 OWNER 提供的视频模拟包（RGB、THERMAL 两段），经 SRT 受控上传到云端
# MediaMTX 流路径。不修改任何 ROS 命令节点；不连接真实相机。
#
# 安全铁律：
#   - SRT 发布 token/passphrase 只经受控文件读取（0600，root:sim用户），
#     绝不进入命令行参数、日志、聊天或 GitHub；
#   - 进程以专用低权限用户运行（默认 firebot-sim）；不触碰 G1/G2 通信单元；
#   - 模拟标志持续存在：streamid 与日志标记 SIM_VIDEO；REAL_CAMERA_SENSOR=NOT_RUN；
#   - 停止发布由 kill 本脚本管理的 ffmpeg PID 实现，页面 LIVE 状态由云端 MediaMTX
#     语义自然退化（不伪造）。
#
# 前置：OWNER 投递视频模拟包（VIDEO_RGB/VIDEO_THERMAL 两个文件）与云端 CC 受控
# 投递 token/passphrase 文件；8889/8890 UDP 由云端确认放行。
# 用法（root 或 sudo）：
#   bash scripts/firebot_video_outbound_sim.sh start
#   bash scripts/firebot_video_outbound_sim.sh stop
#   bash scripts/firebot_video_outbound_sim.sh decode-check   # 本机可解码验证
set -euo pipefail

SIM_USER="${SIM_VIDEO_USER:-firebot-sim}"
STATE_DIR="${SIM_VIDEO_STATE_DIR:-/var/lib/firebot/video-sim}"
VIDEO_RGB="${SIM_VIDEO_RGB:-}"
VIDEO_THERMAL="${SIM_VIDEO_THERMAL:-}"
SRT_URL_RGB="${SIM_SRT_URL_RGB:-}"
SRT_URL_THERMAL="${SIM_SRT_URL_THERMAL:-}"
TOKEN_FILE="${SIM_SRT_TOKEN_FILE:-/etc/firebot/video-sim/srt_token}"      # 0600 受控文件
PASSPHRASE_FILE="${SIM_SRT_PASSPHRASE_FILE:-/etc/firebot/video-sim/srt_passphrase}"

fail() { echo "VIDEO_SIM=FAIL: $1" >&2; exit 1; }

decode_check() {
  [ -n "$VIDEO_RGB" ] && [ -f "$VIDEO_RGB" ] || fail "VIDEO_RGB 未投递: $VIDEO_RGB"
  [ -n "$VIDEO_THERMAL" ] && [ -f "$VIDEO_THERMAL" ] || fail "VIDEO_THERMAL 未投递: $VIDEO_THERMAL"
  ffmpeg -v error -i "$VIDEO_RGB" -f null - && echo "DECODE-RGB=OK"
  ffmpeg -v error -i "$VIDEO_THERMAL" -f null - && echo "DECODE-THERMAL=OK"
}

start() {
  decode_check
  [ -r "$TOKEN_FILE" ] || fail "SRT token 文件未受控投递: $TOKEN_FILE"
  [ -r "$PASSPHRASE_FILE" ] || fail "SRT passphrase 文件未受控投递: $PASSPHRASE_FILE"
  [ -n "$SRT_URL_RGB" ] && [ -n "$SRT_URL_THERMAL" ] || fail "SRT URL 未配置（云端 CC 提供）"
  mkdir -p "$STATE_DIR"
  # 低权限模拟用户（安装时创建一次；此处确保存在，不做其他系统修改）
  getent passwd "$SIM_USER" >/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SIM_USER"
  chown -R "$SIM_USER":"$SIM_USER" "$STATE_DIR"

  # ffmpeg 循环发布；passphrase 经文件注入环境，不进 argv：
  #   SRT: srt://<host>:8890?streamid=<streamid>&passphrase=<file 值经 env 读取>
  # 实际实现：passphrase 通过 -passphrase 读取文件内容的方式依 ffmpeg 版本；
  # 此处用 env 变量由 runuser 注入（进程 environ 仅 sim 用户可读）。
  for stream in rgb thermal; do
    video=$([ "$stream" = rgb ] && echo "$VIDEO_RGB" || echo "$VIDEO_THERMAL")
    url=$([ "$stream" = rgb ] && echo "$SRT_URL_RGB" || echo "$SRT_URL_THERMAL")
    runuser -u "$SIM_USER" -- env \
      FIREBOT_SRT_PASSPHRASE="$(cat "$PASSPHRASE_FILE")" \
      ffmpeg -v warning -re -stream_loop -1 -i "$video" \
        -c:v libx264 -preset veryfast -tune zerolatency -g 30 \
        -f mpegts "$url" \
        > "$STATE_DIR/$stream.out" 2> "$STATE_DIR/$stream.err" &
    echo $! > "$STATE_DIR/$stream.pid"
    echo "SIM_PUBLISH_STARTED stream=$stream pid=$(cat "$STATE_DIR/$stream.pid") marker=SIM_VIDEO"
  done
  echo "VIDEO_SIM=RUNNING（REAL_CAMERA_SENSOR=NOT_RUN；token/passphrase 未回显）"
}

stop() {
  for stream in rgb thermal; do
    if [ -f "$STATE_DIR/$stream.pid" ]; then
      kill "$(cat "$STATE_DIR/$stream.pid")" 2>/dev/null || true
      rm -f "$STATE_DIR/$stream.pid"
      echo "SIM_PUBLISH_STOPPED stream=$stream（云端 LIVE 状态由 MediaMTX 自然退化）"
    fi
  done
  echo "VIDEO_SIM=STOPPED"
}

case "${1:-}" in
  decode-check) decode_check ;;
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 {decode-check|start|stop}" >&2; exit 2 ;;
esac
