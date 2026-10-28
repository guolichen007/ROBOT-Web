#!/usr/bin/env bash
# enroll.sh — firebotctl vehicle enroll 的受控实现（Enrollment 输出到 Edge Home，规格十九）。
#
# 合同不变：Enrollment API / Device Token / MQTT topic-schema 均不改。
# 输出（canonical Edge Home）：
#   config/identity.env、config/assignment.env、config/bridge.env、config/profile.env
#   secrets/mqtt.env（0600）、secrets/device-token（0600）
#   certs/mqtt-ca.crt 由 bootstrap 阶段预置（enroll 只读校验存在）
#
# cutover 门（P0-2 语义）：legacy firebot-bridge 与 firebot-vehicle-edge 任一 active →
# ENROLL=REJECTED；不写任何文件、不消费 token。
# 兼容：FIREBOT_SERVER_URL 缺失时 legacy /etc/firebot/bootstrap.env 只读 discovery。
#
# 用法（root）：
#   enroll.sh <DEVICE_ID> --token <TOKEN> [--server https://…] [--edge-home …]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VEHICLE_EDGE_SRC="$(cd "$SELF_DIR/.." && pwd)"
PROFILE_DIR="${FIREBOT_PROFILE_DIR:-$VEHICLE_EDGE_SRC/config/profiles}"
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
CURL="${FIREBOT_TEST_CURL:-curl}"
LEGACY_BOOTSTRAP="/etc/firebot/bootstrap.env"

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法" >&2
  exit 1
fi
shift

TOKEN=""
SERVER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --server) SERVER="${2:-}"; shift 2 ;;
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$TOKEN" ] || { echo "ERROR: 缺少 --token（服务器 firebotctl fleet register 签发）" >&2; exit 1; }

# bootstrap 来源：Edge Home config/bootstrap.env 优先；legacy /etc/firebot 兼容只读。
BOOTSTRAP_ENV="$EDGE_HOME/config/bootstrap.env"
[ -f "$BOOTSTRAP_ENV" ] || BOOTSTRAP_ENV="$LEGACY_BOOTSTRAP"
if [ -f "$BOOTSTRAP_ENV" ]; then
  # shellcheck disable=SC1090
  . "$BOOTSTRAP_ENV"
fi
[ -n "$SERVER" ] || SERVER="${FIREBOT_ENROLL_URL:-${FIREBOT_SERVER_URL:+${FIREBOT_SERVER_URL%/}/api/v1/enrollment/activate}}"
[ -n "$SERVER" ] || { echo "ERROR: 未配置 enrollment 服务器地址（config/bootstrap.env 的 FIREBOT_SERVER_URL）" >&2; exit 1; }
case "$SERVER" in
  https://*) : ;;
  *) echo "ERROR: enrollment 服务器必须为 https:// 正式域名（禁止 http/IP/Tailscale）" >&2; exit 1 ;;
esac
server_host="$(printf '%s' "$SERVER" | sed -E 's#^https://([^/:]+).*#\1#')"
if printf '%s' "$server_host" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$|^100\.(64|68|110)\.|tailscale'; then
  echo "ERROR: 禁止 IP/Tailscale enrollment 地址" >&2
  exit 1
fi

# ---- cutover 门：token 必须 Bridge/Edge 已停止后才允许消费 ----
legacy_state="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
edge_state="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
if [ "$legacy_state" != "inactive" ] || [ "$edge_state" != "inactive" ]; then
  echo "ENROLL=REJECTED"
  echo "LEGACY_BRIDGE_STILL_ACTIVE"
  echo "原因：firebot-bridge/edge 仍在运行；先 firebotctl vehicle migration-stop-legacy 进入 cutover。" >&2
  exit 1
fi

# 测试逃逸：仅 CI fixture；生产必须 root（secrets 0600 需要）。
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi

# ---- 一次性消费 token（服务器事务化；HTTPS 走系统 CA，禁止 -k/--cacert）----
resp="$("$CURL" -sf -X POST "$SERVER" \
  -H 'Content-Type: application/json' \
  -d "{\"device_id\":\"$DEVICE_ID\",\"token\":\"$TOKEN\"}" 2>/dev/null || true)"
[ -n "$resp" ] || { echo "ENROLL=FAIL（enrollment API 不可达 / token 无效或已使用）" >&2; exit 1; }

# ---- 写入 Edge Home（identity/assignment/secrets；值不回显）----
mkdir -p "$EDGE_HOME/config" "$EDGE_HOME/secrets" "$EDGE_HOME/certs"
chmod 0755 "$EDGE_HOME/config" 2>/dev/null || true
chmod 0700 "$EDGE_HOME/secrets" 2>/dev/null || true
[ -f "$EDGE_HOME/certs/mqtt-ca.crt" ] || { echo "ENROLL=FAIL（certs/mqtt-ca.crt 缺失；bootstrap 阶段未完成）" >&2; exit 1; }

echo "$resp" | FIREBOT_EDGE_HOME="$EDGE_HOME" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
did = d["device_id"]
pid = d.get("profile_id", "firebot_ros1_standard_v1")
home = os.environ["FIREBOT_EDGE_HOME"]
display = d.get("display_name", "")
open(home + "/config/identity.env", "w").write("DEVICE_ID=%s\nPROFILE_ID=%s\nDISPLAY_NAME=%s\n" % (did, pid, display))
os.chmod(home + "/config/identity.env", 0o644)
open(home + "/config/assignment.env", "w").write(
    "MQTT_HOST=%s\nMQTT_PORT=%s\nSITE_CODE=%s\nMAP_CODE=%s\nMAP_VERSION=%s\nMAP_CHECKSUM=%s\n"
    % (d.get("mqtt_host", ""), d.get("mqtt_port", 8883), d.get("site_code", ""),
       d.get("map_code", ""), d.get("map_version", ""), d.get("map_checksum", ""))
)
os.chmod(home + "/config/assignment.env", 0o644)
open(home + "/secrets/mqtt.env", "w").write("FIREBOT_MQTT_PASSWORD=%s\n" % d["mqtt_password"])
os.chmod(home + "/secrets/mqtt.env", 0o600)
open(home + "/secrets/device-token", "w").write(d.get("device_token", ""))
os.chmod(home + "/secrets/device-token", 0o600)
print(did)
print(pid)
'
PROFILE_ID="$(grep -E '^PROFILE_ID=' "$EDGE_HOME/config/identity.env" | cut -d= -f2-)"

# ---- 生成 bridge.env（来源 identity + profile + assignment；缺失 MQTT_HOST 即失败，无 fallback）----
[ -f "$PROFILE_DIR/$PROFILE_ID.yaml" ] || { echo "ERROR: profile 不存在: $PROFILE_ID（$PROFILE_DIR）" >&2; exit 1; }
python3 - "$DEVICE_ID" "$PROFILE_ID" "$PROFILE_DIR/$PROFILE_ID.yaml" "$EDGE_HOME/config/assignment.env" "$EDGE_HOME/config/bridge.env" <<'PY'
import os, sys
device_id, profile_id, profile_path, assignment_path, out_path = sys.argv[1:6]
def load_kv(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d
assignment = load_kv(assignment_path)
if not assignment.get("MQTT_HOST"):
    print("ERROR: assignment 缺少 MQTT_HOST", file=sys.stderr)
    sys.exit(1)
profile = {}
cur = None
for line in open(profile_path, encoding="utf-8"):
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    indent = len(raw) - len(raw.lstrip(" "))
    if indent == 0 and stripped.endswith(":"):
        cur = stripped[:-1]
        profile[cur] = {}
    elif ":" in stripped and indent >= 2:
        k, v = stripped.split(":", 1)
        profile.setdefault(cur, {})[k.strip()] = v.strip()
bridge = profile.get("bridge", {})
lines = [
    "# GENERATED FILE - DO NOT EDIT（来源：identity + fleet profile + server assignment）",
    "# 如需改变 map/site/MQTT host，用 firebotctl vehicle profile-sync 重新生成。",
    "",
    "FIREBOT_VEHICLE_ID=%s" % device_id,
    "FIREBOT_MQTT_USERNAME=%s" % device_id,
    "FIREBOT_MQTT_HOST=%s" % assignment.get("MQTT_HOST", ""),
    "FIREBOT_MQTT_PORT=%s" % assignment.get("MQTT_PORT", "8883"),
    "FIREBOT_CA_CERT=%s" % assignment.get("MQTT_CA", os.environ.get("FIREBOT_EDGE_HOME", "/home/tl/firebot-edge") + "/certs/mqtt-ca.crt"),
    "",
    "FIREBOT_SITE_CODE=%s" % assignment.get("SITE_CODE", ""),
    "FIREBOT_MAP_CODE=%s" % assignment.get("MAP_CODE", ""),
    "FIREBOT_MAP_VERSION=%s" % assignment.get("MAP_VERSION", ""),
    "FIREBOT_MAP_CHECKSUM=%s" % assignment.get("MAP_CHECKSUM", ""),
    "FIREBOT_LOCATION_ENABLED=%s" % assignment.get("LOCATION_ENABLED", "false"),
    "",
    "FIREBOT_HEARTBEAT_HZ=1",
    "FIREBOT_STATUS_HZ=1",
    "FIREBOT_LOCATION_MAX_HZ=10",
    "",
    "BRIDGE_STUB_MODE=false",
    "FIREBOT_FEEDBACK_TIMEOUT_SECONDS=3",
    "",
    "FIREBOT_SUPPORTED_COMMANDS=%s" % assignment.get("SUPPORTED_COMMANDS", ""),
    "FIREBOT_SENSORS=",
    "FIREBOT_MEDIA=",
    "FIREBOT_PROTOCOL_VERSION=1.3.0",
    "",
    "FIREBOT_FIELD_TRACE=false",
    "",
]
open(out_path, "w").write("\n".join(lines))
import os
os.chmod(out_path, 0o644)
PY

echo "ENROLL=PASS device_id=$DEVICE_ID profile=$PROFILE_ID"
echo "EDGE_ENROLLMENT_STORAGE=config/secrets（identity/assignment/bridge/mqtt.env/device-token）"
