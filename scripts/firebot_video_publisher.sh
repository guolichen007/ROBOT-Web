#!/usr/bin/env bash
# firebot-001 真实相机发布器（RGB / THERMAL 各一个独立进程，互不影响）。
#
# 用途：真实相机取流（UVC 或 RTSP）→ H.264 → SRT 受控上传云端。
# 由 systemd 单元 firebot-video-rgb.service / firebot-video-thermal.service 各跑一个实例，
# 相机离线或 SRT 断开时进程退出，systemd 有界退避重启（StartLimit 限制高频重启）。
#
# 安全铁律：
#   - SRT streamid/passphrase 只经 0640 受控文件读取后注入进程环境，
#     绝不进入命令行参数、日志、聊天或 GitHub；
#   - 进程以专用低权限用户运行（默认 firebot-video）；
#   - 输入源不可用直接退出（fail-closed），不用合成源冒充真实相机；
#   - 不触碰 G1/G2 通信单元，不发送任何模拟温度/电池/烟雾数据；
#   - 设备占用铁律（同型车统一）：若车内 ROS 采集节点（camera_sdk / svusb_thermal）
#     已在运行并持有 /dev/videoN，本发布器不得直开同一设备——打不开设备即
#     fail-closed 退出，绝不双开同一 USB 相机。
#
# 本机实测修正（2026-11-18，G4-Fleet-A2）：
#   - 凭证文件权限必须 0640 root:firebot-video：0600 root:firebot-video 经实测
#     firebot-video 用户不可读（0600 属主 root 只有 root 可读），会导致启动即
#     "凭证未投递" 误判。
#   - ffmpeg 4.2.7 的 h264_nvenc 与 NVIDIA 驱动 595.x 不兼容（"Cannot get the
#     preset configuration: unsupported param (12)"，任意 preset 均失败），
#     故本脚本默认使用 libx264 软编（实测 1280x480@30 OK）；升级 ffmpeg>=5.1
#     后可将 ENC 参数切回 h264_nvenc -preset p1 -tune ll。VAAPI 亦不可用
#     （libva 2.7 驱动栈不支持 12 代 iGPU）。
#   - 本机 libsrt 1.4.0 实测 SRT_ENV_PASSPHRASE/SRT_ENV_STREAMID 不注入
#     （getsockflag 读回空），且 srt-live-transmit 1.4.0 不支持 URI 里
#     passphrase 参数（#5003）。生产启用加密前必须升级 libsrt>=1.4.4/1.5，
#     否则凭证会静默失效（链路无加密、无 streamid）。
#   - srt-live-transmit 默认 autoreconnect=yes（对端断开不退出、自动重连）；
#     本脚本显式 -a:no，断连即整体退出，由 systemd 有界退避重启（长期故障恢复）。
#
# 前置（等云端交付后启用）：
#   /etc/firebot/video/env            0640 root:firebot-video（非敏感配置）
#   /etc/firebot/video/srt_passphrase 0640 root:firebot-video（SRT 加密 passphrase）
#   /etc/firebot/video/srt_streamid   0640 root:firebot-video（streamid，含发布 token）
#   /etc/firebot/video/rtsp_url_rgb   0640 root:firebot-video（RTSP 凭证时使用，UVC 不需要）
#
# 用法：bash scripts/firebot_video_publisher.sh rgb|thermal
set -euo pipefail

MODE="${1:-}"
[ "$MODE" = "rgb" ] || [ "$MODE" = "thermal" ] || { echo "usage: $0 {rgb|thermal}" >&2; exit 2; }

ETC_DIR="${FIREBOT_VIDEO_ETC:-/etc/firebot/video}"
STATE_DIR="${FIREBOT_VIDEO_STATE_DIR:-/var/lib/firebot/video}"
ENV_FILE="$ETC_DIR/env"
PASSPHRASE_FILE="$ETC_DIR/srt_passphrase"
STREAMID_FILE="$ETC_DIR/srt_streamid"

# ---------- 配置（env 文件 + 环境变量覆盖） ----------
[ -r "$ENV_FILE" ] || { echo "VIDEO_PUBLISH=FAIL: env 文件未受控投递: $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

if [ "$MODE" = rgb ]; then
  CAM_DEV="${RGB_DEV:-/dev/video0}"
  SRT_HOST="${RGB_SRT_HOST:-}"
  SRT_PORT="${RGB_SRT_PORT:-}"
  CAM_WIDTH="${RGB_WIDTH:-1280}"
  CAM_HEIGHT="${RGB_HEIGHT:-480}"
  CAM_FPS="${RGB_FPS:-30}"
  CAM_PIXFMT="${RGB_PIXFMT:-mjpeg}"
  CAM_RTSP="${RGB_RTSP_URL:-}"
  UDP_PORT="${RGB_UDP_PORT:-8891}"
  STREAM_MARKER="REAL_RGB"
else
  CAM_DEV="${THERMAL_DEV:-/dev/video1}"
  SRT_HOST="${THERMAL_SRT_HOST:-}"
  SRT_PORT="${THERMAL_SRT_PORT:-}"
  CAM_WIDTH="${THERMAL_WIDTH:-640}"
  CAM_HEIGHT="${THERMAL_HEIGHT:-480}"
  CAM_FPS="${THERMAL_FPS:-25}"
  CAM_PIXFMT="${THERMAL_PIXFMT:-yuyv422}"
  CAM_RTSP="${THERMAL_RTSP_URL:-}"
  UDP_PORT="${THERMAL_UDP_PORT:-8892}"
  STREAM_MARKER="REAL_THERMAL"
fi

# ---------- 输入可用性（fail-closed） ----------
if [ -n "$CAM_RTSP" ]; then
  SOURCE_IN="rtsp://$CAM_RTSP"
  timeout 8 ffprobe -v error -i "$SOURCE_IN" -show_entries stream=codec_name -of csv=p=0 >/dev/null 2>&1 \
    || { echo "VIDEO_PUBLISH=FAIL: RTSP 输入不可达（$MODE）" >&2; exit 1; }
  STREAM_COPY=1   # RTSP 已是 H.264 码流：优先 -c copy 转封装
else
  [ -e "$CAM_DEV" ] || { echo "VIDEO_PUBLISH=FAIL: 相机设备不存在: $CAM_DEV（$MODE）" >&2; exit 1; }
  SOURCE_IN=""
  STREAM_COPY=0
fi

[ -r "$PASSPHRASE_FILE" ] || { echo "VIDEO_PUBLISH=FAIL: SRT passphrase 未受控投递" >&2; exit 1; }
[ -r "$STREAMID_FILE" ] || { echo "VIDEO_PUBLISH=FAIL: SRT streamid 未受控投递" >&2; exit 1; }
[ -n "$SRT_HOST" ] && [ -n "$SRT_PORT" ] || { echo "VIDEO_PUBLISH=FAIL: SRT endpoint 未配置（等云端交付）" >&2; exit 1; }

mkdir -p "$STATE_DIR"

# ---------- 凭证经 env 注入（不进 argv） ----------
SRT_PASSPHRASE="$(cat "$PASSPHRASE_FILE")"
SRT_STREAMID="$(cat "$STREAMID_FILE")"
export SRT_ENV_PASSPHRASE="$SRT_PASSPHRASE"     # libsrt 1.4 env 配置，进程 environ 仅服务用户可读
export SRT_ENV_STREAMID="$SRT_STREAMID"
unset SRT_PASSPHRASE SRT_STREAMID

# ---------- 双进程管道：ffmpeg 取流编码 → UDP 回环 → srt-live-transmit 出站 ----------
# 任一端退出即整体退出（set -e + wait 检测），systemd 有界退避重启。
FFMPEG_PID=""
SRT_PID=""
cleanup() {
  [ -n "$FFMPEG_PID" ] && kill "$FFMPEG_PID" 2>/dev/null || true
  [ -n "$SRT_PID" ] && kill "$SRT_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "VIDEO_PUBLISH=START mode=$MODE marker=$STREAM_MARKER REAL_CAMERA_SENSOR=REAL"
if [ "$STREAM_COPY" = 1 ]; then
  ffmpeg -v warning -re -i "$SOURCE_IN" -c:v copy -an -f mpegts "udp://127.0.0.1:${UDP_PORT}?pkt_size=1316" &
else
  # 实测编码器矩阵（2026-11-18）：libx264 veryfast 可用；h264_nvenc 在
  # ffmpeg 4.2.7+驱动 595 下任意 preset 均报 unsupported param(12)；
  # h264_vaapi 因 libva 2.7 驱动栈不支持 12 代 iGPU 而失败。
  ffmpeg -v warning -f v4l2 -input_format "$CAM_PIXFMT" \
    -video_size "${CAM_WIDTH}x${CAM_HEIGHT}" -framerate "$CAM_FPS" -i "$CAM_DEV" \
    -c:v libx264 -preset veryfast -b:v 2M -g $((CAM_FPS * 2)) -an \
    -f mpegts "udp://127.0.0.1:${UDP_PORT}?pkt_size=1316" &
fi
FFMPEG_PID=$!
sleep 2   # 等 ffmpeg 起流

# -a:no：SRT 对端断开即退出（默认 autoreconnect=yes 会无限重连不退出），
# 断连后由 systemd 有界退避重启，避免掩盖长期故障。
srt-live-transmit -a:no "udp://127.0.0.1:${UDP_PORT}" "srt://${SRT_HOST}:${SRT_PORT}" \
  -st:yes -chunk:1316 -linger:2 > "$STATE_DIR/$MODE-srt.out" 2> "$STATE_DIR/$MODE-srt.err" &
SRT_PID=$!

# 等待任一进程退出；退出时 set -e 之外显式判定
wait -n "$FFMPEG_PID" "$SRT_PID" 2>/dev/null || true
EXIT_CODE=$?
echo "VIDEO_PUBLISH=STOP mode=$MODE rc=$EXIT_CODE（systemd 将按有界退避处理）"
# srt-live-transmit 断连时退出码为 0，但发布管道任一端退出都应触发
# systemd Restart=on-failure 的有界退避重启，统一转成非 0。
[ "$EXIT_CODE" = 0 ] && EXIT_CODE=1
exit "$EXIT_CODE"
