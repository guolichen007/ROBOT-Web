#!/usr/bin/env bash
# readonly-preflight.sh（Vehicle Edge 版）— firebotctl vehicle readonly-preflight 的受控实现。
#
# 与 legacy 版同语义，路径/service 迁移到 Edge Home：
#   bootstrap：允许尚未 enrollment（检查 OS/ROS/repo/bootstrap 文件/DNS/HTTPS 系统 CA/
#              MQTT TLS 握手/NTP/控制面全关/legacy+edge 双 inactive）
#   enrolled ：bootstrap 子集 + Edge Home config/secrets/certs + EXACT_SHA_MATCH 强制 +
#              FIREBOT_SUPPORTED_COMMANDS 必须为空
#
# 铁律：不得 MQTT publish；不得使用生产 credential 登录（MQTT 检查仅 TLS 握手）。
#       任何一项 FAIL → 整体 FAIL（fail-closed）。
#
# 用法（车端）：
#   readonly-preflight.sh --phase bootstrap --device-id <ID> [--expected-sha <SHA>]
#   readonly-preflight.sh --phase enrolled  [--expected-sha <SHA>]
set -euo pipefail

PHASE=""
DEVICE_ID=""
EXPECTED_SHA=""
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
REPO_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:-}"; shift 2 ;;
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
case "$PHASE" in
  bootstrap|enrolled) ;;
  *) echo "用法: $0 --phase bootstrap|enrolled [--device-id <ID>] [--expected-sha <SHA>]" >&2; exit 2 ;;
esac
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -n "$REPO_DIR" ] || REPO_DIR="$(cd "$SELF_DIR/../.." && pwd)"

CONFIG="$EDGE_HOME/config"
SECRETS="$EDGE_HOME/secrets"
CERTS="$EDGE_HOME/certs"
SERVER_URL="https://firebot.youdaozd.com"
MQTT_HOST="firebot.youdaozd.com"
MQTT_PORT="8883"

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
read_kv() {
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
  fail "REPO_CLEAN=no"
else
  pass "REPO_CLEAN=yes"
fi
if [ -z "$EXPECTED_SHA" ]; then
  EXPECTED_SHA="$(read_kv "$EDGE_HOME/config/release.env" FINAL_SHA)"
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
  DEVICE_ID="$(read_kv "$CONFIG/identity.env" DEVICE_ID)"
fi
if [ -z "$DEVICE_ID" ] || ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  fail "EXPECTED_DEVICE_ID=no（必须提供 --device-id）"
else
  pass "EXPECTED_DEVICE_ID=$DEVICE_ID"
fi
if [ -f "$CONFIG/identity.env" ]; then
  env_did="$(read_kv "$CONFIG/identity.env" DEVICE_ID)"
  [ -n "$env_did" ] || fail "IDENTITY_ENV 缺少 DEVICE_ID"
  [ "$env_did" = "$DEVICE_ID" ] && pass "IDENTITY_ID_MATCH=yes" \
    || fail "IDENTITY_ID_MATCH=no（identity=$env_did 期望=$DEVICE_ID）"
fi

# ---- bootstrap 文件 / 信任链（Edge Home canonical；legacy /etc/firebot 兼容只读）----
BOOTSTRAP_ENV="$CONFIG/bootstrap.env"
[ -f "$BOOTSTRAP_ENV" ] || BOOTSTRAP_ENV="/etc/firebot/bootstrap.env"
if [ -f "$BOOTSTRAP_ENV" ] && [ "$(read_kv "$BOOTSTRAP_ENV" FIREBOT_SERVER_URL)" = "$SERVER_URL" ]; then
  pass "BOOTSTRAP_ENV=yes FIREBOT_SERVER_URL=$SERVER_URL"
else
  fail "BOOTSTRAP_ENV=no（必须含 FIREBOT_SERVER_URL=$SERVER_URL；禁止 IP/HTTP/Tailscale）"
fi
[ -f "$CERTS/mqtt-ca.crt" ] && pass "MQTT_CA_EXISTS=yes" || fail "MQTT_CA_EXISTS=no（缺少 $CERTS/mqtt-ca.crt）"

# ---- DNS / HTTPS 系统 CA / MQTT TLS 握手（不发布任何 MQTT 消息）----
if [ -n "$("$GETENT" hosts "$MQTT_HOST" 2>/dev/null)" ]; then
  pass "DNS_RESOLVE=yes $MQTT_HOST"
else
  fail "DNS_RESOLVE=no $MQTT_HOST"
fi
if "$CURL" -sf -o /dev/null --max-time 15 "$SERVER_URL/" 2>/dev/null; then
  pass "HTTPS_SYSTEM_CA=PASS"
else
  fail "HTTPS_SYSTEM_CA=FAIL（系统 CA 验证失败；禁止 -k/--cacert 绕过）"
fi
mqtt_verify="$(echo | timeout 15 "$OPENSSL" s_client -connect "$MQTT_HOST:$MQTT_PORT" \
  -servername "$MQTT_HOST" -CAfile "$CERTS/mqtt-ca.crt" 2>/dev/null | grep -c 'Verify return code: 0' || true)"
[ "$mqtt_verify" -ge 1 ] && pass "MQTT_TLS_HOSTNAME=PASS $MQTT_HOST:$MQTT_PORT" \
  || fail "MQTT_TLS_HOSTNAME=FAIL"

# ---- NTP / 系统时钟 ----
ntp_ok=no
[ "$("$TIMEDATECTL" show -p NTPSynchronized 2>/dev/null | cut -d= -f2)" = "yes" ] && ntp_ok=yes
[ "$("$SYSTEMCTL" is-active systemd-timesyncd 2>/dev/null)" = "active" ] && ntp_ok=yes
[ "$ntp_ok" = "yes" ] && pass "NTP_SANE=yes" || fail "NTP_SANE=no"

# ---- 控制面全关 + legacy/new 互斥（只读铁律）----
ctl_enabled="$("$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active="$("$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
[ "$ctl_enabled" = "enabled" ] && fail "CONTROL_ENABLED=yes" || pass "CONTROL_ENABLED=no"
case "$ctl_active" in
  active|activating|reloading) fail "CONTROL_ACTIVE=yes" ;;
  *) pass "CONTROL_ACTIVE=no" ;;
esac
legacy_state="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
edge_state="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
if [ "$legacy_state" = "inactive" ] && [ "$edge_state" = "inactive" ]; then
  pass "COMMUNICATION_OWNER=NONE（legacy+edge 双 inactive）"
else
  fail "COMMUNICATION_OWNER=CONFLICT（legacy=$legacy_state edge=$edge_state；LEGACY_AND_EDGE_MUTUAL_EXCLUSION）"
fi
if "$PGREP" -f "rosmaster|roscore" >/dev/null 2>&1; then
  fail "ROS_MASTER_INACTIVE=no"
else
  pass "ROS_MASTER_INACTIVE=yes"
fi
if "$PGREP" -f "roslaunch|move_base" >/dev/null 2>&1; then
  fail "BASE_NAV_INACTIVE=no"
else
  pass "BASE_NAV_INACTIVE=yes"
fi

# ---- enrolled phase 特有 ----
if [ "$PHASE" = "enrolled" ]; then
  if [ -n "$EXPECTED_SHA" ] && echo "$EXPECTED_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    head_sha="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
    if [ "$head_sha" = "$EXPECTED_SHA" ]; then
      pass "EXACT_SHA_MATCH=YES"
    else
      fail "EXACT_SHA_MATCH=NO（HEAD=$head_sha 期望=$EXPECTED_SHA）"
    fi
  fi
  [ -f "$SECRETS/mqtt.env" ] && [ "$(stat -c %a "$SECRETS/mqtt.env" 2>/dev/null)" = "600" ] \
    && pass "MQTT_ENV=yes mode=600" || fail "MQTT_ENV=no（$SECRETS/mqtt.env 必须存在且 mode=600）"
  [ -f "$CONFIG/assignment.env" ] && pass "ASSIGNMENT_ENV=yes" || fail "ASSIGNMENT_ENV=no"

  [ "$(read_kv "$CONFIG/bridge.env" FIREBOT_MQTT_HOST)" = "$MQTT_HOST" ] \
    && pass "MQTT_HOST=$MQTT_HOST" || fail "MQTT_HOST 必须 $MQTT_HOST"
  [ "$(read_kv "$CONFIG/bridge.env" FIREBOT_MQTT_PORT)" = "$MQTT_PORT" ] \
    && pass "MQTT_PORT=$MQTT_PORT" || fail "MQTT_PORT 必须 $MQTT_PORT"
  [ "$(read_kv "$CONFIG/bridge.env" FIREBOT_CA_CERT)" = "$CERTS/mqtt-ca.crt" ] \
    && pass "MQTT_CA=$CERTS/mqtt-ca.crt" || fail "MQTT_CA 必须 $CERTS/mqtt-ca.crt"

  sc="$(read_kv "$CONFIG/bridge.env" FIREBOT_SUPPORTED_COMMANDS)"
  if [ -n "$sc" ]; then
    fail "SUPPORTED_COMMANDS_NOT_EMPTY（FIREBOT_SUPPORTED_COMMANDS=$sc）"
  else
    pass "SUPPORTED_COMMANDS_EMPTY=yes"
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  [ "$PHASE" = "bootstrap" ] && echo "READONLY_BOOTSTRAP_PREFLIGHT=FAIL" \
    || echo "READONLY_ENROLLED_PREFLIGHT=FAIL"
  exit 1
fi
[ "$PHASE" = "bootstrap" ] && echo "READONLY_BOOTSTRAP_PREFLIGHT=PASS" \
  || echo "READONLY_ENROLLED_PREFLIGHT=PASS"
