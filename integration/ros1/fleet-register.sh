#!/usr/bin/env bash
# fleet-register.sh — 服务器侧：为新设备签发 per-device MQTT credential + 一次性 enrollment token
#
# 用法（管理员在服务器上执行）：sudo ./fleet-register.sh <DEVICE_ID>
# 前提：DEVICE_ID 已在服务器 Robot 表预登记（firebotctl fleet status 可查）。
#
# 安全模型：
#   - per-device credential（绝不 fleet 共用密码）：username=DEVICE_ID + 随机 password
#   - 一次性 enrollment token（32 字节随机 hex），仅用于车辆首次 enroll，用完即弃
#   - credential 写入 Mosquitto password 文件；token 由管理员经 Tailscale/一次性交付车辆，不落 Git
set -euo pipefail

DEVICE_ID="${1:-}"
[ -n "$DEVICE_ID" ] || { echo "用法: fleet-register.sh <DEVICE_ID>" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（sudo）" >&2; exit 1; }

MOSQUITTO_PASSWD_FILE="${FIREBOT_MOSQUITTO_PASSWD_FILE:-/opt/firebot/secrets/mosquitto/passwords}"
ENROLL_DIR="${FIREBOT_ENROLL_DIR:-/opt/firebot/enrollment}"
mkdir -p "$(dirname "$MOSQUITTO_PASSWD_FILE")" "$ENROLL_DIR"

# per-device password + 一次性 token（绝不 fleet 共用）
PASSWORD="$(openssl rand -hex 24)"
ENROLL_TOKEN="$(openssl rand -hex 32)"

# 写入 Mosquitto password 文件（username = DEVICE_ID，per-device credential）
if command -v mosquitto_passwd >/dev/null 2>&1; then
  mosquitto_passwd -b "$MOSQUITTO_PASSWD_FILE" "$DEVICE_ID" "$PASSWORD"
  echo "  Mosquitto credential 已写入（mosquitto_passwd 哈希）: $DEVICE_ID"
else
  # fallback：仅 lab；生产必须用 mosquitto_passwd（避免明文）
  printf '%s:%s\n' "$DEVICE_ID" "$PASSWORD" >> "$MOSQUITTO_PASSWD_FILE"
  echo "  WARN: 无 mosquitto_passwd，fallback 明文写入（仅 lab，生产禁止）" >&2
fi

# 一次性 enrollment token（600 权限，不落 Git）
printf '%s\n' "$ENROLL_TOKEN" > "$ENROLL_DIR/$DEVICE_ID.token"
chmod 600 "$ENROLL_DIR/$DEVICE_ID.token"

echo ""
echo "FLEET_REGISTER=PASS device_id=$DEVICE_ID"
echo "MQTT_USERNAME=$DEVICE_ID"
echo "ENROLL_TOKEN=$ENROLL_TOKEN"
echo "  交付方式：管理员经 Tailscale/一次性通道把 token 交给现场；车辆执行："
echo "    firebotctl vehicle enroll $DEVICE_ID --token $ENROLL_TOKEN"
