#!/usr/bin/env bash
# fleet-register.sh — 服务器侧：为新设备签发 per-device MQTT credential + 一次性 enrollment token
#
# 用法（管理员在服务器上执行）：sudo ./fleet-register.sh <DEVICE_ID>
#
# 安全模型：
#   - DEVICE_ID 严格 ^[A-Za-z0-9_-]{1,64}$
#   - 必须已在服务器 Robot 表预登记，否则 REJECTED
#   - per-device credential（username=DEVICE_ID + 随机 password），绝不 fleet 共用
#   - token + credential 存 PostgreSQL（DB 事务化消费），不依赖 host/container 文件同步
set -euo pipefail

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法（只允许 [A-Za-z0-9_-]{1,64}）" >&2
  exit 1
fi
[ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（sudo）" >&2; exit 1; }

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

# ---- 1) 确认设备已在服务器 Robot 表预登记 ----
registered="$("${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
print('YES' if r else 'NO')
" 2>/dev/null || echo "NO")"
[ "$registered" = "YES" ] || { echo "FLEET_REGISTER=REJECTED（$DEVICE_ID 未在服务器 Robot 表预登记）" >&2; exit 1; }

# ---- 2) per-device credential ----
command -v mosquitto_passwd >/dev/null 2>&1 || {
  echo "FLEET_REGISTER=FAIL（缺少 mosquitto_passwd，生产禁止明文 credential）" >&2
  exit 1
}
MOSQUITTO_PASSWD_FILE="${FIREBOT_MOSQUITTO_PASSWD_FILE:-./secrets/mosquitto/passwords}"
mkdir -p "$(dirname "$MOSQUITTO_PASSWD_FILE")"
PASSWORD="$(openssl rand -hex 24)"
mosquitto_passwd -b "$MOSQUITTO_PASSWD_FILE" "$DEVICE_ID" "$PASSWORD"

# ---- 3) credential 真正生效（config-init 同步 source → volume → restart → healthy → TLS 验证）----
CA_CERT="${FIREBOT_MOSQUITTO_CA:-./secrets/mosquitto/certs/ca.crt}"
MQTT_PORT="$("${COMPOSE[@]}" port mosquitto 8883 2>/dev/null | sed 's/.*://' || echo 8883)"
# 重新跑 mosquitto-config-init，把 source 的 passwords + ACL 复制进 mosquitto_config volume
"${COMPOSE[@]}" run --rm mosquitto-config-init
# 重启 mosquitto 并等 healthy（禁止 restart || true）
"${COMPOSE[@]}" restart mosquitto
healthy="no"
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" ps --format '{{.Name}} {{.Health}}' 2>/dev/null | grep -qE 'mosquitto[^-].*healthy'; then
    healthy="yes"; break
  fi
  sleep 2
done
[ "$healthy" = "yes" ] || { echo "FLEET_REGISTER=FAIL（mosquitto 未 healthy）" >&2; exit 1; }
# 新 credential TLS publish 测试（本设备 namespace，禁止未授权 /health）
if mosquitto_pub -h 127.0.0.1 -p "$MQTT_PORT" --cafile "$CA_CERT" \
     -u "$DEVICE_ID" -P "$PASSWORD" \
     -t "robot/$DEVICE_ID/heartbeat" -m ping -q 1 2>/dev/null; then
  echo "  credential 激活测试（TLS connect + publish robot/$DEVICE_ID/heartbeat）：PASS"
else
  echo "FLEET_REGISTER=FAIL（credential 激活测试失败：broker/ACL/CA 未就绪）" >&2
  exit 1
fi

# ---- 4) token + credential 存 DB（一次性，重放拒绝由 DB 事务化消费保证）----
ENROLL_TOKEN="$(openssl rand -hex 32)"
DEVICE_TOKEN="$(openssl rand -hex 32)"
PROFILE_ID="${FIREBOT_PROFILE_ID:-firebot_ros1_standard_v1}"
MQTT_HOST="${FIREBOT_MQTT_HOST:-100.110.31.112}"
SITE_CODE="${FIREBOT_SITE_CODE:-}"
MAP_CODE="${FIREBOT_MAP_CODE:-}"
MAP_VERSION="${FIREBOT_MAP_VERSION:-}"
MAP_CHECKSUM="${FIREBOT_MAP_CHECKSUM:-}"

"${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.modules.enrollment.token_store import issue_token
from app.db.models import RobotFleetAssignment
import hashlib
db = SessionLocal()
cred = {
    'profile_id': '$PROFILE_ID',
    'mqtt_host': '$MQTT_HOST',
    'mqtt_port': $MQTT_PORT,
    'mqtt_password': '$PASSWORD',
    'device_token': '$DEVICE_TOKEN',
    'ca_cert': '/etc/firebot/production-ca.crt',
    'site_code': '$SITE_CODE',
    'map_code': '$MAP_CODE',
    'map_version': '$MAP_VERSION',
    'map_checksum': '$MAP_CHECKSUM',
}
token = issue_token(db, '$DEVICE_ID', cred)
# 设备 API token：存 SHA-256 哈希到 RobotFleetAssignment（用于 assignment 变更鉴权）
from app.db.models import Robot
from sqlalchemy import select
robot = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
if robot:
    a = db.get(RobotFleetAssignment, robot.id)
    if not a:
        a = RobotFleetAssignment(robot_id=robot.id)
        db.add(a)
    a.device_token_hash = hashlib.sha256('$DEVICE_TOKEN'.encode()).hexdigest()
db.commit()
print(token)
" > /tmp/firebot-enroll-token.txt
ENROLL_TOKEN="$(cat /tmp/firebot-enroll-token.txt)"
rm -f /tmp/firebot-enroll-token.txt

echo ""
echo "FLEET_REGISTER=PASS device_id=$DEVICE_ID"
echo "MQTT_USERNAME=$DEVICE_ID"
echo "ENROLL_TOKEN=$ENROLL_TOKEN"
echo "DEVICE_TOKEN=$DEVICE_TOKEN"
echo "  交付：管理员经 Tailscale/一次性通道把 token 交给现场；车辆执行 firebotctl vehicle enroll $DEVICE_ID --token $ENROLL_TOKEN"
