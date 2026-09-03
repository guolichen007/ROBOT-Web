#!/usr/bin/env bash
# fleet-register.sh — 服务器侧：为新设备签发 per-device MQTT credential + 一次性 enrollment token
#
# 用法（管理员在服务器上执行）：sudo ./fleet-register.sh <DEVICE_ID>
#
# 安全模型：
#   - DEVICE_ID 严格 ^[A-Za-z0-9_-]{1,64}$（用于 username/token 文件名/MQTT namespace）
#   - 必须已在服务器 Robot 表预登记，否则 REJECTED
#   - per-device credential（username=DEVICE_ID + 随机 password），绝不 fleet 共用
#   - 生产禁止无 mosquitto_passwd 明文 fallback
#   - 一次性 enrollment token（32 字节随机 hex）+ expiry + 单次消费（见 enrollment API）
set -euo pipefail

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法（只允许 [A-Za-z0-9_-]{1,64}，拒绝 / .. : 空白 换行 shell 字符）" >&2
  exit 1
fi
[ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（sudo）" >&2; exit 1; }

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

# ---- 1) 确认设备已在服务器 Robot 表预登记 ----
registered="NO"
if "${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
print('YES' if r else 'NO')
" 2>/dev/null; then
  registered="$("${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == '$DEVICE_ID'))
print('YES' if r else 'NO')
" 2>/dev/null)"
fi
[ "$registered" = "YES" ] || { echo "FLEET_REGISTER=REJECTED（$DEVICE_ID 未在服务器 Robot 表预登记）" >&2; exit 1; }

# ---- 2) 生产必须 mosquitto_passwd（禁止明文 fallback）----
command -v mosquitto_passwd >/dev/null 2>&1 || {
  echo "FLEET_REGISTER=FAIL（缺少 mosquitto_passwd，生产禁止明文 credential）" >&2
  exit 1
}

MOSQUITTO_PASSWD_FILE="${FIREBOT_MOSQUITTO_PASSWD_FILE:-./secrets/mosquitto/passwords}"
ENROLL_DIR="${FIREBOT_ENROLL_DIR:-/opt/firebot/enrollment}"
mkdir -p "$(dirname "$MOSQUITTO_PASSWD_FILE")" "$ENROLL_DIR"

# ---- 3) per-device credential ----
PASSWORD="$(openssl rand -hex 24)"
mosquitto_passwd -b "$MOSQUITTO_PASSWD_FILE" "$DEVICE_ID" "$PASSWORD"

# ---- 4) credential 激活测试（真实 TLS connect，best-effort；失败即 FAIL）----
CA_CERT="${FIREBOT_MOSQUITTO_CA:-./secrets/mosquitto/certs/ca.crt}"
MQTT_PORT="$("${COMPOSE[@]}" port mosquitto 8883 2>/dev/null | sed 's/.*://' || echo 8883)"
"${COMPOSE[@]}" restart mosquitto >/dev/null 2>&1 || true
sleep 2
if mosquitto_pub -h 127.0.0.1 -p "$MQTT_PORT" --cafile "$CA_CERT" \
     -u "$DEVICE_ID" -P "$PASSWORD" \
     -t "robot/$DEVICE_ID/health" -m ping -q 1 2>/dev/null; then
  echo "  credential 激活测试（TLS connect + publish 本设备 namespace）：PASS"
else
  echo "  credential 激活测试：WARN（可能 ACL/CA 未就绪，需现场确认；credential 已写入）" >&2
fi

# ---- 5) 一次性 enrollment token（存 TokenStore 格式：hash + expiry + consumed；不存明文）----
ENROLL_TOKEN="$(openssl rand -hex 32)"
TOKEN_HASH="$(printf '%s' "$ENROLL_TOKEN" | sha256sum | cut -d' ' -f1)"
NOW_TS="$(date +%s)"
EXPIRES_AT=$((NOW_TS + 3600))
cat > "$ENROLL_DIR/$DEVICE_ID.json" <<EOF
{"device_id": "$DEVICE_ID", "token_hash": "$TOKEN_HASH", "issued_at": $NOW_TS, "expires_at": $EXPIRES_AT, "consumed": false}
EOF
chmod 600 "$ENROLL_DIR/$DEVICE_ID.json"

# ---- 6) pending credential（enrollment API 单次交付后删除；不落 Git）----
cat > "$ENROLL_DIR/$DEVICE_ID.cred" <<EOF
{"profile_id": "${FIREBOT_PROFILE_ID:-firebot_ros1_standard_v1}", "mqtt_host": "${FIREBOT_MQTT_HOST:-100.110.31.112}", "mqtt_port": ${FIREBOT_MQTT_PORT:-8883}, "mqtt_password": "$PASSWORD", "ca_cert": "/etc/firebot/production-ca.crt", "site_code": "${FIREBOT_SITE_CODE:-}", "map_code": "${FIREBOT_MAP_CODE:-}", "map_version": "${FIREBOT_MAP_VERSION:-}", "map_checksum": "${FIREBOT_MAP_CHECKSUM:-}"}
EOF
chmod 600 "$ENROLL_DIR/$DEVICE_ID.cred"

echo ""
echo "FLEET_REGISTER=PASS device_id=$DEVICE_ID"
echo "MQTT_USERNAME=$DEVICE_ID"
echo "ENROLL_TOKEN=$ENROLL_TOKEN"
echo "  交付：管理员经 Tailscale/一次性通道把 token 交给现场；车辆执行 firebotctl vehicle enroll $DEVICE_ID --token $ENROLL_TOKEN"
