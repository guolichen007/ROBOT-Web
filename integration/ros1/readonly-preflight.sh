#!/usr/bin/env bash
# readonly-preflight.sh — 真实车辆只读接入前置检查（firebotctl vehicle readonly-preflight 的实现）。
#
# 两个 phase（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE / P0-D）：
#   bootstrap：允许尚未 enrollment。检查 OS/ROS/repo/bootstrap 文件/DNS/HTTPS 系统 CA/
#              MQTT TLS 握手/NTP/控制面全关（Control/ROS Master/Base/Navigation）。
#   enrolled ：bootstrap 子集 + device.env/bridge.env（MQTT host/port/CA）/bridge-secret.env 0600/
#              assignment.env / FIREBOT_SUPPORTED_COMMANDS 必须为空。
#
# 铁律：不得 MQTT publish；不得使用生产 credential 登录（MQTT 检查仅 TLS 握手）。
#       任何一项 FAIL → 整体 FAIL（fail-closed）。
#
# 用法（车端）：
#   firebotctl vehicle readonly-preflight --phase bootstrap --device-id <DEVICE_ID>
#   firebotctl vehicle readonly-preflight --phase enrolled
set -euo pipefail

PHASE=""
DEVICE_ID="${1:-}"
EXPECTED_SHA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
case "$PHASE" in
  bootstrap|enrolled) ;;
  *) echo "用法: $0 --phase bootstrap|enrolled [--device-id <ID>] [--expected-sha <SHA>]" >&2; exit 2 ;;
esac

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/../.." && pwd)"
# 车端固定环境根（测试可经 FIREBOT_TEST_ETC_ROOT 注入 fixture；生产默认 /etc/firebot）
FIREBOT_ETC="${FIREBOT_TEST_ETC_ROOT:-/etc/firebot}"
DEVICE_ENV="$FIREBOT_ETC/device.env"
BOOTSTRAP_ENV="$FIREBOT_ETC/bootstrap.env"
BRIDGE_ENV="$FIREBOT_ETC/bridge.env"
BRIDGE_SECRET="$FIREBOT_ETC/bridge-secret.env"
ASSIGNMENT_ENV="$FIREBOT_ETC/assignment.env"
MQTT_CA="$FIREBOT_ETC/mqtt-ca.crt"
RELEASE_ENV="$FIREBOT_ETC/release.env"
SERVER_URL="https://firebot.youdaozd.com"
MQTT_HOST="firebot.youdaozd.com"
MQTT_PORT="8883"

# 允许测试注入 fixture 命令/文件（生产调用不设置，走真实命令）。
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
CURL="${FIREBOT_TEST_CURL:-curl}"
OPENSSL="${FIREBOT_TEST_OPENSSL:-openssl}"
GETENT="${FIREBOT_TEST_GETENT:-getent}"
TIMEDATECTL="${FIREBOT_TEST_TIMEDATECTL:-timedatectl}"
OS_RELEASE="${FIREBOT_TEST_OS_RELEASE:-/etc/os-release}"
ROS_NOETIC_SETUP="${FIREBOT_TEST_ROS_NOETIC_SETUP:-/opt/ros/noetic/setup.bash}"
PGREP="${FIREBOT_TEST_PGREP:-pgrep}"

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }

read_kv() { # read_kv <file> <key> → 值（无则空）
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}

# ---- 基础环境 ----
if grep -qE '^ID=ubuntu$' "$OS_RELEASE" 2>/dev/null; then
  v="$(grep -E '^VERSION_ID=' "$OS_RELEASE" | cut -d= -f2- | tr -d '"')"
  [ "$v" = "20.04" ] && pass "OS=ubuntu-20.04" || fail "OS 版本必须 Ubuntu 20.04（实际 $v）"
else
  fail "OS 必须 Ubuntu 20.04"
fi

if [ -f "$ROS_NOETIC_SETUP" ]; then
  pass "ROS_NOETIC_INSTALLED=yes"
else
  rosver="$(command -v rosversion >/dev/null 2>&1 && rosversion -d 2>/dev/null || echo missing)"
  [ "$rosver" = "noetic" ] && pass "ROS_NOETIC_INSTALLED=yes" || fail "ROS Noetic 未安装（rosversion=$rosver）"
fi

# ---- repo clean + approved SHA ----
if [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]; then
  fail "REPO_CLEAN=no（ROBOT-Web 工作区不干净，禁止接车）"
else
  pass "REPO_CLEAN=yes"
fi
if [ -z "$EXPECTED_SHA" ]; then
  EXPECTED_SHA="$(read_kv "$RELEASE_ENV" FINAL_SHA)"
fi
if [ -z "$EXPECTED_SHA" ]; then
  EXPECTED_SHA="${FIREBOT_EXPECTED_SHA:-}"
fi
if [ -z "$EXPECTED_SHA" ] || ! echo "$EXPECTED_SHA" | grep -qE '^[0-9a-f]{40}$'; then
  fail "APPROVED_SHA_PRESENT=no（需 --expected-sha <40hex> 或 release.env FINAL_SHA）"
else
  if git -C "$REPO_DIR" cat-file -e "$EXPECTED_SHA^{commit}" 2>/dev/null; then
    pass "APPROVED_SHA_PRESENT=yes sha=$EXPECTED_SHA"
  else
    fail "APPROVED_SHA_PRESENT=no（$EXPECTED_SHA 不在本地仓库，先 git fetch）"
  fi
fi

# ---- expected device id ----
if [ -z "$DEVICE_ID" ]; then
  DEVICE_ID="$(read_kv "$DEVICE_ENV" DEVICE_ID)"
fi
if [ -z "$DEVICE_ID" ] || ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  fail "EXPECTED_DEVICE_ID=no（必须提供 --device-id）"
else
  pass "EXPECTED_DEVICE_ID=$DEVICE_ID"
fi
if [ -f "$DEVICE_ENV" ]; then
  env_did="$(read_kv "$DEVICE_ENV" DEVICE_ID)"
  [ -n "$env_did" ] || fail "DEVICE_ENV 缺少 DEVICE_ID"
  [ "$env_did" = "$DEVICE_ID" ] && pass "DEVICE_ENV_ID_MATCH=yes" \
    || fail "DEVICE_ENV_ID_MATCH=no（device.env=$env_did 期望=$DEVICE_ID）"
fi

# ---- bootstrap 文件 / 信任链 ----
if [ -f "$BOOTSTRAP_ENV" ] && [ "$(read_kv "$BOOTSTRAP_ENV" FIREBOT_SERVER_URL)" = "$SERVER_URL" ]; then
  pass "BOOTSTRAP_ENV=yes FIREBOT_SERVER_URL=$SERVER_URL"
else
  fail "BOOTSTRAP_ENV=no（必须含 FIREBOT_SERVER_URL=$SERVER_URL；禁止 IP/HTTP/Tailscale）"
fi
[ -f "$MQTT_CA" ] && pass "MQTT_CA_EXISTS=yes" || fail "MQTT_CA_EXISTS=no（缺少 $MQTT_CA）"

# ---- DNS / HTTPS 系统 CA / MQTT TLS 握手 ----
if [ -n "$("$GETENT" hosts "$MQTT_HOST" 2>/dev/null)" ]; then
  pass "DNS_RESOLVE=yes $MQTT_HOST"
else
  fail "DNS_RESOLVE=no $MQTT_HOST"
fi
# HTTPS = Ubuntu 系统 CA store（无 --cacert / -k）
if "$CURL" -sf -o /dev/null --max-time 15 "$SERVER_URL/" 2>/dev/null; then
  pass "HTTPS_SYSTEM_CA=PASS"
else
  fail "HTTPS_SYSTEM_CA=FAIL（系统 CA 验证失败；禁止 -k/--cacert 绕过）"
fi
# MQTT 仅 TLS 握手验证 hostname（不发布消息、不使用生产 credential）
mqtt_verify="$(echo | timeout 15 "$OPENSSL" s_client -connect "$MQTT_HOST:$MQTT_PORT" \
  -servername "$MQTT_HOST" -CAfile "$MQTT_CA" 2>/dev/null | grep -c 'Verify return code: 0' || true)"
[ "$mqtt_verify" -ge 1 ] && pass "MQTT_TLS_HOSTNAME=PASS $MQTT_HOST:$MQTT_PORT" \
  || fail "MQTT_TLS_HOSTNAME=FAIL（$MQTT_CA 验证 $MQTT_HOST:$MQTT_PORT 失败）"

# ---- NTP / 系统时钟 ----
ntp_ok=no
[ "$("$TIMEDATECTL" show -p NTPSynchronized 2>/dev/null | cut -d= -f2)" = "yes" ] && ntp_ok=yes
[ "$("$SYSTEMCTL" is-active systemd-timesyncd 2>/dev/null)" = "active" ] && ntp_ok=yes
[ "$ntp_ok" = "yes" ] && pass "NTP_SANE=yes" || fail "NTP_SANE=no（时钟不同步会破坏 TLS/JWT/命令过期）"

# ---- 控制面全关（只读铁律）----
ctl_enabled="$("$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active="$("$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
[ "$ctl_enabled" = "enabled" ] && fail "CONTROL_ENABLED=yes（firebot-control enabled，只读铁律违反）" \
  || pass "CONTROL_ENABLED=no"
case "$ctl_active" in
  active|activating|reloading) fail "CONTROL_ACTIVE=yes（firebot-control $ctl_active，只读铁律违反）" ;;
  *) pass "CONTROL_ACTIVE=no" ;;
esac
if "$PGREP" -f "rosmaster|roscore" >/dev/null 2>&1; then
  fail "ROS_MASTER_INACTIVE=no（检测到 ROS Master 进程）"
else
  pass "ROS_MASTER_INACTIVE=yes"
fi
if "$PGREP" -f "roslaunch|move_base" >/dev/null 2>&1; then
  fail "BASE_NAV_INACTIVE=no（检测到 roslaunch/move_base 进程）"
else
  pass "BASE_NAV_INACTIVE=yes"
fi

# ---- enrolled phase 特有 ----
if [ "$PHASE" = "enrolled" ]; then
  # P0-5 cutover 事务门：enrolled 阶段要求工作区 HEAD == EXPECTED_SHA（exact SHA）。
  # bootstrap 阶段不强制（迁移前允许仍处于旧 release）。
  if [ -n "$EXPECTED_SHA" ] && echo "$EXPECTED_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    head_sha="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
    if [ "$head_sha" = "$EXPECTED_SHA" ]; then
      pass "EXACT_SHA_MATCH=YES"
    else
      fail "EXACT_SHA_MATCH=NO（HEAD=$head_sha 期望=$EXPECTED_SHA）"
    fi
  fi
  if [ -f "$BRIDGE_SECRET" ] && [ "$(stat -c %a "$BRIDGE_SECRET" 2>/dev/null)" = "600" ]; then
    pass "BRIDGE_SECRET_ENV=yes mode=600"
  else
    fail "BRIDGE_SECRET_ENV=no（$BRIDGE_SECRET 必须存在且 mode=600）"
  fi
  [ -f "$ASSIGNMENT_ENV" ] && pass "ASSIGNMENT_ENV=yes" || fail "ASSIGNMENT_ENV=no"

  # bridge.env：MQTT host/port/CA 与生产语义一致
  [ "$(read_kv "$BRIDGE_ENV" FIREBOT_MQTT_HOST)" = "$MQTT_HOST" ] \
    && pass "MQTT_HOST=$MQTT_HOST" || fail "MQTT_HOST 必须 $MQTT_HOST（bridge.env 不符或缺失）"
  [ "$(read_kv "$BRIDGE_ENV" FIREBOT_MQTT_PORT)" = "$MQTT_PORT" ] \
    && pass "MQTT_PORT=$MQTT_PORT" || fail "MQTT_PORT 必须 $MQTT_PORT"
  [ "$(read_kv "$BRIDGE_ENV" FIREBOT_CA_CERT)" = "$MQTT_CA" ] \
    && pass "MQTT_CA=$MQTT_CA" || fail "MQTT_CA 必须 $MQTT_CA"

  # SUPPORTED_COMMANDS 必须为空（patrol/stop_motion/manual_control 任一存在 → FAIL）
  sc="$(read_kv "$BRIDGE_ENV" FIREBOT_SUPPORTED_COMMANDS)"
  if [ -n "$sc" ]; then
    fail "SUPPORTED_COMMANDS_NOT_EMPTY（FIREBOT_SUPPORTED_COMMANDS=$sc）"
  else
    pass "SUPPORTED_COMMANDS_EMPTY=yes"
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  if [ "$PHASE" = "bootstrap" ]; then
    echo "READONLY_BOOTSTRAP_PREFLIGHT=FAIL"
  else
    echo "READONLY_ENROLLED_PREFLIGHT=FAIL"
  fi
  exit 1
fi
if [ "$PHASE" = "bootstrap" ]; then
  echo "READONLY_BOOTSTRAP_PREFLIGHT=PASS"
else
  echo "READONLY_ENROLLED_PREFLIGHT=PASS"
fi
