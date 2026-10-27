#!/usr/bin/env bash
# fleet-register.sh — 服务器侧（U-Dao 生产）：为已预登记设备签发一次性 enrollment token。
#
# 生产语义（REAL_VEHICLE_READONLY_PREP，与 U-Dao 公网部署统一）：
#   - MQTT_HOST=firebot.youdaozd.com / MQTT_PORT=8883，来源已验收的
#     runtime/env/public-activation.env（可经 U_DAO_FLEET_REGISTER_HOST/PORT 显式覆盖，仅限测试）。
#     正式公网 host 缺失 → FLEET_REGISTER=FAIL，绝不 fallback 到 Tailscale/IP（旧 100.110.31.112 语义已废弃）。
#   - MQTT credential 唯一权威工具：deploy/u-dao/scripts/60-onboard-vehicle.sh <ID> --apply
#     （ensure 语义：已存在 → 复用不旋转；不存在 → 生成 + 卷同步 + SIGHUP + live ACL verify）。
#     本脚本不再自建 mosquitto_passwd、不写旧 ./secrets、不 restart mosquitto、不从 compose port 推断端口。
#   - 读取 canonical credential（runtime/secrets/vehicles/<ID>/mqtt_password）仅用于
#     enrollment token payload：不回显、不写日志、不入 Git、不入 support bundle。
#   - 只输出 FLEET_REGISTER=* 与 ENROLL_TOKEN（一次性）；禁止打印 MQTT password/device token/SRT 凭据。
#
# 用法（服务器上，ROBOT-Web 仓库根）：
#   firebotctl fleet register <DEVICE_ID>
set -euo pipefail

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法（只允许 [A-Za-z0-9_-]{1,64}）" >&2
  exit 1
fi

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
ENV_FILE="${SERVER_ENV_FILE:-$RUNTIME_ROOT/env/.env.server}"
ACTIVATION_ENV="$RUNTIME_ROOT/env/public-activation.env"

# shellcheck source=scripts/docker_cmd.sh
source "$REPO_ROOT/scripts/docker_cmd.sh"
docker_cmd_detect || { echo "FLEET_REGISTER=FAIL（docker 不可用）" >&2; exit 1; }

# ---- 1) U-Dao 生产环境断言：public-activation.env 必须存在（fail-closed）----
[ -s "$ACTIVATION_ENV" ] || {
  echo "FLEET_REGISTER=FAIL（缺少已验收的 $ACTIVATION_ENV；非 U-Dao 生产环境不得执行）" >&2
  exit 1
}
read_activation() {
  grep -E "^$1=" "$ACTIVATION_ENV" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d '[:space:]' || true
}
# MQTT host：测试覆盖用 U_DAO_FLEET_REGISTER_HOST > activation MQTT_PUBLIC_HOSTNAME > PUBLIC_DOMAIN；
# 缺失或含私网/Tailscale/占位特征 → FAIL（不得 fallback）。
MQTT_HOST="${U_DAO_FLEET_REGISTER_HOST:-}"
[ -n "$MQTT_HOST" ] || MQTT_HOST="$(read_activation MQTT_PUBLIC_HOSTNAME)"
[ -n "$MQTT_HOST" ] || MQTT_HOST="$(read_activation PUBLIC_DOMAIN)"
if [ -z "$MQTT_HOST" ] || printf '%s' "$MQTT_HOST" | grep -Eq '100\.110\.|100\.68\.|100\.64\.|192\.168\.|10\.|172\.1[6-9]\.|172\.2[0-9]\.|127\.|localhost|tailscale|example|REPLACE_|CHANGE_ME'; then
  echo "FLEET_REGISTER=FAIL（正式公网 MQTT host 缺失或为 Tailscale/IP/占位：$MQTT_HOST）" >&2
  exit 1
fi
MQTT_PORT="${U_DAO_FLEET_REGISTER_PORT:-8883}"
echo "$MQTT_PORT" | grep -qE '^[0-9]{1,5}$' || { echo "FLEET_REGISTER=FAIL（MQTT_PORT 非法）" >&2; exit 1; }

# ---- 2) 确认设备已在服务器 Robot 表预登记 ----
COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in "$REPO_ROOT/docker-compose.server.yml" "$REPO_ROOT/deploy/u-dao/compose.u-dao.yml"; do
  COMPOSE+=(-f "$f")
done
registered="$("${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
print('YES' if r else 'NO')
" 2>/dev/null || echo "NO")"
[ "$registered" = "YES" ] || { echo "FLEET_REGISTER=REJECTED（$DEVICE_ID 未在服务器 Robot 表预登记）" >&2; exit 1; }

# ---- 3) canonical onboarding ensure（复用已存在凭据，不旋转；缺失才生成；绝不 restart broker）----
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
  bash "$REPO_ROOT/deploy/u-dao/scripts/60-onboard-vehicle.sh" "$DEVICE_ID" --apply \
  || { echo "FLEET_REGISTER=FAIL（canonical onboarding 失败）" >&2; exit 1; }

# ---- 4) 零重启 + 不旋转断言 ----
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

# ---- 5) 读取 canonical credential（仅用于 token payload，不回显）----
PASSWORD="$(cat "$VEHICLE_DIR/mqtt_password")"

# ---- 6) 签发一次性 enrollment token（DB 事务化消费；device token 只存哈希）----
DEVICE_TOKEN="$(openssl rand -hex 32)"
PROFILE_ID="${FIREBOT_PROFILE_ID:-firebot_ros1_standard_v1}"
SITE_CODE="${FIREBOT_SITE_CODE:-}"
MAP_CODE="${FIREBOT_MAP_CODE:-}"
MAP_VERSION="${FIREBOT_MAP_VERSION:-}"
MAP_CHECKSUM="${FIREBOT_MAP_CHECKSUM:-}"

TOKEN_TMP="$(mktemp /tmp/firebot-enroll-token.XXXXXX)"
chmod 600 "$TOKEN_TMP"
"${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.modules.enrollment.token_store import issue_token
from app.db.models import RobotFleetAssignment, Robot
from sqlalchemy import select
import hashlib
db = SessionLocal()
cred = {
    'profile_id': '$PROFILE_ID',
    'mqtt_host': '$MQTT_HOST',
    'mqtt_port': $MQTT_PORT,
    'mqtt_password': '$PASSWORD',
    'device_token': '$DEVICE_TOKEN',
    'ca_cert': '/etc/firebot/mqtt-ca.crt',
    'site_code': '$SITE_CODE',
    'map_code': '$MAP_CODE',
    'map_version': '$MAP_VERSION',
    'map_checksum': '$MAP_CHECKSUM',
}
token = issue_token(db, '$DEVICE_ID', cred)
robot = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
if robot:
    a = db.get(RobotFleetAssignment, robot.id)
    if not a:
        a = RobotFleetAssignment(robot_id=robot.id)
        db.add(a)
    a.device_token_hash = hashlib.sha256('$DEVICE_TOKEN'.encode()).hexdigest()
db.commit()
print(token)
" > "$TOKEN_TMP"
ENROLL_TOKEN="$(cat "$TOKEN_TMP")"
rm -f "$TOKEN_TMP"

echo ""
echo "FLEET_REGISTER=PASS device_id=$DEVICE_ID"
echo "MQTT_HOST=$MQTT_HOST"
echo "MQTT_PORT=$MQTT_PORT"
echo "ENROLL_TOKEN=$ENROLL_TOKEN"
echo "  交付：管理员经安全通道把 enrollment token 交给现场；车辆执行 firebotctl vehicle enroll $DEVICE_ID --token <TOKEN>"
echo "  注：设备长期 API token（device_token）由 enrollment API 一次性安全下发给车辆，不在本输出打印。"
