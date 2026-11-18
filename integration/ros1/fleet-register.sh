#!/usr/bin/env bash
# fleet-register.sh — 服务器侧（U-Dao 生产）：为已预登记设备准备凭证并签发一次性 enrollment token。
#
# G4-Fleet-R2 Transport Profile 语义（fail-closed，无任何默认 host/port）：
#   - 接入参数唯一来源 = 批准的 Transport Profile（deploy/u-dao/transport-profiles.json，
#     解析器 deploy/u-dao/scripts/transport_profile.py）：host/port/protocol/CA/broker_ref/adapter/
#     credential provisioner 必须来自同一 Profile。已映射车辆不得改用其他 Profile；新车必须显式
#     --transport-profile 且该 Profile 允许分配。
#   - MQTT 凭证只经该 Profile 的 credential provisioner 写入对应 Broker；Profile 无 provisioner
#     （例如 001 的 r001-independent-v1：G1 凭证由独立流程签发）→ FLEET_REGISTER=REJECTED，
#     不生成/不轮换 MQTT 密码、不签发 token。
#   - 媒体凭证（per-vehicle SRT token + 平台共享 passphrase）经 60-onboard-vehicle.sh 派生，
#     随 enrollment 一次性下发；主密钥永不下发。
#   - 一次性 enrollment token 只写入 0600 文件（--token-out，默认
#     runtime/secrets/vehicles/<ID>/enroll-token），绝不打印到 stdout/日志/支持包。
#   - device token：新 token 的哈希放在 enrollment 记录中，**车辆成功 activate 后才替换**
#     assignment.device_token_hash——重复 register 不会让车上正在使用的 device token 失效。
#
# 用法（服务器上，ROBOT-Web 仓库根）：
#   firebotctl fleet register <DEVICE_ID> [--transport-profile <ID>] [--token-out <FILE>]
#                             [--media-srt-host <H>] [--media-srt-port <P>] [--confirm-protected]
set -euo pipefail

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法（只允许 [A-Za-z0-9_-]{1,64}）" >&2
  exit 1
fi
shift
PROFILE_ARG=""
TOKEN_OUT=""
MEDIA_SRT_HOST=""
MEDIA_SRT_PORT=""
CONFIRM_PROTECTED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --transport-profile) PROFILE_ARG="${2:-}"; shift 2 ;;
    --token-out) TOKEN_OUT="${2:-}"; shift 2 ;;
    --media-srt-host) MEDIA_SRT_HOST="${2:-}"; shift 2 ;;
    --media-srt-port) MEDIA_SRT_PORT="${2:-}"; shift 2 ;;
    --confirm-protected) CONFIRM_PROTECTED=1; shift ;;
    *) echo "ERROR: 未知参数 $1" >&2; exit 1 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
ENV_FILE="${SERVER_ENV_FILE:-$RUNTIME_ROOT/env/.env.server}"
ACTIVATION_ENV="$RUNTIME_ROOT/env/public-activation.env"
REGISTRY="${FIREBOT_TRANSPORT_PROFILES:-$REPO_ROOT/deploy/u-dao/transport-profiles.json}"

# shellcheck source=scripts/docker_cmd.sh
source "$REPO_ROOT/scripts/docker_cmd.sh"
docker_cmd_detect || { echo "FLEET_REGISTER=FAIL（docker 不可用）" >&2; exit 1; }

# ---- 1) U-Dao 生产环境断言：public-activation.env 必须存在（fail-closed）----
[ -s "$ACTIVATION_ENV" ] || {
  echo "FLEET_REGISTER=FAIL（缺少已验收的 $ACTIVATION_ENV；非 U-Dao 生产环境不得执行）" >&2
  exit 1
}

# ---- 2) Transport Profile 解析（host/port/protocol/CA/broker_ref/adapter/provisioner 一致）----
TP_JSON="$(python3 "$REPO_ROOT/deploy/u-dao/scripts/transport_profile.py" resolve "$DEVICE_ID" \
  ${PROFILE_ARG:+--profile "$PROFILE_ARG"} --registry "$REGISTRY" \
  --activation-env "$ACTIVATION_ENV" --require-provisioner)" || {
  echo "FLEET_REGISTER=REJECTED（Transport Profile 解析失败；未生成/未轮换任何凭证、未签发 token）" >&2
  exit 1
}
tp() { printf '%s' "$TP_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin)[sys.argv[1]]; print("" if v is None else v)' "$1"; }
TRANSPORT_PROFILE="$(tp transport_profile)"
MQTT_HOST="$(tp mqtt_host)"
MQTT_PORT="$(tp mqtt_port)"
PROVISIONER_CMD="$(tp provisioner_command)"
BROKER_REF="$(tp broker_ref)"
case "$PROVISIONER_CMD" in
  deploy/u-dao/scripts/60-onboard-vehicle.sh) : ;;
  *) echo "FLEET_REGISTER=REJECTED（未知 credential provisioner: $PROVISIONER_CMD）" >&2; exit 1 ;;
esac
echo "TRANSPORT_PROFILE=$TRANSPORT_PROFILE broker_ref=$BROKER_REF mqtt=$MQTT_HOST:$MQTT_PORT"
# 受保护车辆（如实际 firebot-vehicle-02）：重跑 register 会改写生产 password_file 并 SIGHUP 主 Broker，
# 必须由运维显式 --confirm-protected 确认（防误操作影响在线车辆）。
if [ "$(tp protected)" = "True" ] && [ "$CONFIRM_PROTECTED" != 1 ]; then
  echo "FLEET_REGISTER=REJECTED（$DEVICE_ID 为受保护车辆；确认影响后加 --confirm-protected）" >&2
  exit 1
fi

# 媒体 SRT endpoint：显式参数 > activation（MEDIA_SRT_PUBLIC_HOSTNAME/PUBLIC_DOMAIN + MEDIA_SRT_PUBLIC_PORT）
read_activation() {
  grep -E "^$1=" "$ACTIVATION_ENV" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d '[:space:]' || true
}
[ -n "$MEDIA_SRT_HOST" ] || MEDIA_SRT_HOST="$(read_activation MEDIA_SRT_PUBLIC_HOSTNAME)"
[ -n "$MEDIA_SRT_HOST" ] || MEDIA_SRT_HOST="$(read_activation PUBLIC_DOMAIN)"
[ -n "$MEDIA_SRT_PORT" ] || MEDIA_SRT_PORT="$(read_activation MEDIA_SRT_PUBLIC_PORT)"
echo "$MEDIA_SRT_HOST" | grep -qE '^[A-Za-z0-9.-]{1,253}$' || { echo "FLEET_REGISTER=FAIL（媒体 SRT host 缺失）" >&2; exit 1; }
echo "$MEDIA_SRT_PORT" | grep -qE '^[0-9]{1,5}$' || { echo "FLEET_REGISTER=FAIL（媒体 SRT 端口缺失：--media-srt-port 或 activation MEDIA_SRT_PUBLIC_PORT）" >&2; exit 1; }

# ---- 3) 确认设备已在服务器 Robot 表预登记（参数经 stdin，不拼进代码）----
COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in "$REPO_ROOT/docker-compose.server.yml" "$REPO_ROOT/deploy/u-dao/compose.u-dao.yml"; do
  COMPOSE+=(-f "$f")
done
registered="$(printf '%s' "$DEVICE_ID" | "${COMPOSE[@]}" exec -T api python -c '
import sys
from sqlalchemy import select
from app.db.models import Robot
from app.db.session import SessionLocal
vid = sys.stdin.read().strip()
with SessionLocal() as db:
    print("YES" if db.scalar(select(Robot).where(Robot.vehicle_id == vid)) else "NO")
' 2>/dev/null || echo "NO")"
[ "$registered" = "YES" ] || { echo "FLEET_REGISTER=REJECTED（$DEVICE_ID 未在服务器 Robot 表预登记）" >&2; exit 1; }

# ---- 4) canonical onboarding ensure（复用已存在凭据，不旋转；缺失才生成；绝不 restart broker）----
VEHICLE_DIR="$RUNTIME_ROOT/secrets/vehicles/$DEVICE_ID"
CRED_EXISTED=NO
PW_BEFORE=""
if [ -f "$VEHICLE_DIR/mqtt_password" ]; then
  CRED_EXISTED=YES
  PW_BEFORE="$(sha256sum "$VEHICLE_DIR/mqtt_password" | awk '{print $1}')"
fi
MOSQ_CID="$("${DOCKER[@]}" ps -q -f "label=com.docker.compose.project=u-dao-firebot" \
  -f "label=com.docker.compose.service=mosquitto" 2>/dev/null | head -1)"
STARTED_AT_BEFORE="$("${DOCKER[@]}" inspect --format '{{.State.StartedAt}}' "$MOSQ_CID" 2>/dev/null || echo UNKNOWN)"

U_DAO_REPO_ROOT="$REPO_ROOT" U_DAO_RUNTIME_ROOT="$RUNTIME_ROOT" \
  bash "$REPO_ROOT/$PROVISIONER_CMD" "$DEVICE_ID" --apply \
  || { echo "FLEET_REGISTER=FAIL（canonical onboarding 失败）" >&2; exit 1; }
# 媒体凭证 bundle（同一 provisioner；token 确定性派生，幂等；MQTT 不动）
U_DAO_REPO_ROOT="$REPO_ROOT" U_DAO_RUNTIME_ROOT="$RUNTIME_ROOT" \
  bash "$REPO_ROOT/$PROVISIONER_CMD" "$DEVICE_ID" --media-only >/dev/null \
  || { echo "FLEET_REGISTER=FAIL（媒体凭证派生失败）" >&2; exit 1; }

# ---- 5) 零重启 + 不旋转断言 ----
STARTED_AT_AFTER="$("${DOCKER[@]}" inspect --format '{{.State.StartedAt}}' "$MOSQ_CID" 2>/dev/null || echo UNKNOWN)"
if [ "$STARTED_AT_BEFORE" != "$STARTED_AT_AFTER" ] || [ "$STARTED_AT_AFTER" = "UNKNOWN" ]; then
  echo "FLEET_REGISTER=FAIL（MOSQUITTO_ZERO_RESTART 不成立：broker StartedAt 发生变化）" >&2
  exit 1
fi
echo "MOSQUITTO_ZERO_RESTART=PASS"
if [ "$CRED_EXISTED" = "YES" ]; then
  PW_AFTER="$(sha256sum "$VEHICLE_DIR/mqtt_password" | awk '{print $1}')"
  if [ "$PW_BEFORE" != "$PW_AFTER" ]; then
    echo "FLEET_REGISTER=FAIL（$DEVICE_ID MQTT credential 被旋转，禁止）" >&2
    exit 1
  fi
  echo "${DEVICE_ID}_EXISTING_CREDENTIAL_REUSED=PASS"
  if [ "$DEVICE_ID" = "R001" ]; then
    echo "R001_MQTT_CREDENTIAL_ROTATED=NO"
  fi
else
  echo "${DEVICE_ID}_CREDENTIAL_GENERATED=PASS（首次生成，经 canonical onboarding）"
fi

# ---- 6) 组装 enrollment payload（host 侧 python 读取 0600 凭证文件；值不进 argv/shell 变量）
#         → stdin 管道送入容器 python 签发一次性 token（只存哈希）。
PROFILE_ID="${FIREBOT_PROFILE_ID:-firebot_ros1_standard_v1}"
umask 077
mkdir -p "$VEHICLE_DIR"
[ -n "$TOKEN_OUT" ] || TOKEN_OUT="$VEHICLE_DIR/enroll-token"
TOKEN_TMP="$(mktemp "$(dirname "$TOKEN_OUT")/.enroll-token.XXXXXX")"
chmod 600 "$TOKEN_TMP"
# G1-GO0 A：异常退出也必须清理 Token 临时文件（trap 覆盖失败/中断路径）
trap 'rm -f "$TOKEN_TMP"' EXIT
FIREBOT_SITE_CODE="${FIREBOT_SITE_CODE:-}" FIREBOT_MAP_CODE="${FIREBOT_MAP_CODE:-}" \
FIREBOT_MAP_VERSION="${FIREBOT_MAP_VERSION:-}" FIREBOT_MAP_CHECKSUM="${FIREBOT_MAP_CHECKSUM:-}" \
python3 -c '
import json, os, secrets, sys
vid, vdir, profile_id, tp_json, srt_host, srt_port = sys.argv[1:7]
tp = json.loads(tp_json)
rd = lambda n: open(os.path.join(vdir, n), encoding="utf-8").read().strip()
payload = {
    "device_id": vid,
    "profile_id": profile_id,
    "transport_profile": tp["transport_profile"],
    "protocol": tp["protocol"],
    "broker_ref": tp["broker_ref"],
    "mqtt_host": tp["mqtt_host"],
    "mqtt_port": int(tp["mqtt_port"]),
    "ca_vehicle_path": tp["ca_vehicle_path"],
    "mqtt_password": rd("mqtt_password"),
    "device_token": secrets.token_hex(32),
    "site_code": os.environ.get("FIREBOT_SITE_CODE", ""),
    "map_code": os.environ.get("FIREBOT_MAP_CODE", ""),
    "map_version": os.environ.get("FIREBOT_MAP_VERSION", ""),
    "map_checksum": os.environ.get("FIREBOT_MAP_CHECKSUM", ""),
    "media": {
        "kind": "firebot.media-credentials.v1",
        "vehicle_id": vid,
        "srt_host": srt_host,
        "srt_port": int(srt_port),
        "srt_publish_token": rd("srt_publish_token"),
        "srt_publish_passphrase": rd("srt_publish_passphrase"),
    },
}
json.dump(payload, sys.stdout)
' "$DEVICE_ID" "$VEHICLE_DIR" "$PROFILE_ID" "$TP_JSON" "$MEDIA_SRT_HOST" "$MEDIA_SRT_PORT" \
| "${COMPOSE[@]}" exec -T api python -c '
import json
import sys

from app.db.session import SessionLocal
from app.modules.enrollment.token_store import issue_token

payload = json.load(sys.stdin)
with SessionLocal.begin() as db:
    # device token 只在车辆成功 activate 后替换 assignment.device_token_hash（见 enrollment router）
    print(issue_token(db, payload["device_id"], payload))
' > "$TOKEN_TMP"
[ -s "$TOKEN_TMP" ] || { echo "FLEET_REGISTER=FAIL（enrollment token 签发失败）" >&2; exit 1; }
mv -f "$TOKEN_TMP" "$TOKEN_OUT"
chmod 600 "$TOKEN_OUT"
trap - EXIT

echo ""
echo "FLEET_REGISTER=PASS device_id=$DEVICE_ID transport_profile=$TRANSPORT_PROFILE"
echo "MQTT_HOST=$MQTT_HOST"
echo "MQTT_PORT=$MQTT_PORT"
echo "MEDIA_SRT=$MEDIA_SRT_HOST:$MEDIA_SRT_PORT"
echo "ENROLL_TOKEN_FILE=$TOKEN_OUT（0600；一次性；值不回显、不入日志/支持包）"
echo "  交付：经安全通道把该文件交给现场；车辆执行"
echo "    firebotctl vehicle enroll $DEVICE_ID --token-file <文件>   （或 --token-stdin 交互输入）"
echo "  注：设备长期 API token 与媒体凭证由 enrollment API 一次性下发；车辆 activate 成功前旧 device token 继续有效。"
