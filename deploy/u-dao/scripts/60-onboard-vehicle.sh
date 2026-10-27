#!/usr/bin/env bash
# U-Dao 平台侧单车凭据生成与激活（语义对应 firebotctl vehicle onboard；车端 firebotctl 流程不变）。
#
# 幂等语义：
#   默认（无 flag）   只生成凭据；已存在 → FAIL（提示 --apply 或 --force）
#   --apply           ensure：不存在则生成，存在则不旋转；随后
#                     password_file 更新 → 同步 mosquitto_config 卷 → SIGHUP 原地 reload
#                     → verify → broker 行为级 live ACL verify（61-verify-vehicle-acl.py）
#   --force           明确旋转凭据（旧车端 credential 失效警告）+ 更新 + 同步 + SIGHUP + verify
#
# 生成（全部只写 runtime，不入 Git，不回显）：
#   <id> MQTT username（== vehicle_id，与 acl.example pattern 约定一致）
#   <id> MQTT password（追加 runtime/secrets/mosquitto/passwords）
#   <id> SRT publish token（HMAC-SHA256(master, "vehicle-srt:"+id) 确定性派生）
#   <id> SRT 链路加密 passphrase（平台共享副本，PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE）
# ACL 无需修改：acl.example 的 `pattern %u` 自动按 username 隔离。
#
# 用法：
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R002
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R002 --apply
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R002 --force
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R002 --verify
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R002 --resync-srt
#     （P0-B：master media_publish_token / srt_publish_passphrase 旋转后，重派生本车
#       SRT token 与 passphrase 副本；不触碰 MQTT 凭据、不 reload broker）
set -euo pipefail

VEHICLE_ID="${1:-}"
MODE="${2:-}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
MOSQ_IMG="eclipse-mosquitto:2.1.2-alpine"
ACL_IMAGE="${ACL_CHECK_IMAGE:-firebot-python-test:u-dao}"
VEHICLE_DIR="$RUNTIME_ROOT/secrets/vehicles/$VEHICLE_ID"

fail() { echo "ONBOARD=FAIL: $1" >&2; exit 1; }

[ -n "$VEHICLE_ID" ] || fail "用法: $0 <VEHICLE_ID> [--force|--verify|--apply]"
echo "$VEHICLE_ID" | grep -qE '^[A-Za-z0-9_-]+$' || fail "vehicle_id 含非法字符: $VEHICLE_ID"
[ -d "$RUNTIME_ROOT/secrets" ] || fail "runtime secrets 不存在（先执行 02-gen-bootstrap-secrets.sh）"

detect_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v docker >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    fail "docker 不可用"
  fi
}

credentials_exist() {
  [ -f "$VEHICLE_DIR/mqtt_password" ] && [ -f "$VEHICLE_DIR/srt_publish_token" ]
}

# ---- 生成凭据（真值不入 Git、不回显）----
generate_credentials() {
  umask 077
  mkdir -p "$VEHICLE_DIR"
  chmod 700 "$VEHICLE_DIR"
  MQTT_PW="$(openssl rand -base64 32)"
  printf '%s' "$MQTT_PW" > "$VEHICLE_DIR/mqtt_password"
  MASTER_TOKEN="$(cat "$RUNTIME_ROOT/secrets/app/media_publish_token")"
  SRT_TOKEN="$(printf 'vehicle-srt:%s' "$VEHICLE_ID" | \
    openssl dgst -sha256 -hmac "$MASTER_TOKEN" | awk '{print $2}')"
  printf '%s' "$SRT_TOKEN" > "$VEHICLE_DIR/srt_publish_token"
  cp "$RUNTIME_ROOT/secrets/app/srt_publish_passphrase" "$VEHICLE_DIR/srt_publish_passphrase"
  chmod 600 "$VEHICLE_DIR/mqtt_password" "$VEHICLE_DIR/srt_publish_token" "$VEHICLE_DIR/srt_publish_passphrase"
  {
    echo "# $VEHICLE_ID 凭据交付说明"
    echo "mqtt_password        = $VEHICLE_ID 的 MQTT 密码（MQTT username == vehicle_id）"
    echo "srt_publish_token    = per-vehicle SRT 发布 token（HMAC 派生，身份/发布授权隔离）"
    echo "srt_publish_passphrase = PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE（平台共享链路加密，非 per-vehicle）"
  } > "$VEHICLE_DIR/README.txt"
  chmod 600 "$VEHICLE_DIR/README.txt"
}

# ---- 更新 password_file（mosquitto_passwd；容器内 root 产物 chown 回 ubuntu）----
update_password_file() {
  detect_docker
  "${DOCKER[@]}" run --rm -v "$RUNTIME_ROOT/secrets/mosquitto:/work:rw" "$MOSQ_IMG" \
    mosquitto_passwd -b /work/passwords "$VEHICLE_ID" "$(cat "$VEHICLE_DIR/mqtt_password")" 2>/dev/null
  sudo -n chown ubuntu:ubuntu "$RUNTIME_ROOT/secrets/mosquitto/passwords" || fail "chown passwords 失败"
  chmod 640 "$RUNTIME_ROOT/secrets/mosquitto/passwords"
}

# ---- 同步 mosquitto 运行卷（broker 从 mosquitto_config 卷读文件）----
sync_config_volume() {
  detect_docker
  "${DOCKER[@]}" run --rm \
    -v "$RUNTIME_ROOT/secrets/mosquitto:/source:ro" \
    -v u-dao-firebot_mosquitto_config:/target \
    busybox:1.37.0-musl sh -c 'mkdir -p /target/certs &&
      cp /source/passwords /target/passwords &&
      cp /source/acl /target/acl &&
      cp /source/certs/ca.crt /target/certs/ca.crt &&
      cp /source/certs/server.crt /target/certs/server.crt &&
      cp /source/certs/server.key /target/certs/server.key &&
      chown -R 1883:1883 /target &&
      chmod 750 /target /target/certs &&
      chmod 640 /target/passwords /target/acl /target/certs/ca.crt /target/certs/server.crt /target/certs/server.key' \
    || fail "mosquitto 配置卷同步失败"
  echo "CONFIG_VOLUME_SYNC=PASS"
}

# ---- SIGHUP 原地 reload（label 精确定位；0/>1 容器 FAIL；不重启 broker）----
reload_broker() {
  detect_docker
  local cids count
  cids="$("${DOCKER[@]}" ps -q -f "label=com.docker.compose.project=u-dao-firebot" \
           -f "label=com.docker.compose.service=mosquitto" 2>/dev/null || true)"
  count="$(printf '%s\n' "$cids" | grep -c .)"
  [ "$count" -eq 1 ] || fail "mosquitto 容器数=$count（必须恰好 1 个）"
  "${DOCKER[@]}" kill --signal HUP "$cids" || fail "SIGHUP 发送失败"
  sleep 2
  "${DOCKER[@]}" inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cids" \
    | grep -qE '^true( healthy)?$' || fail "HUP 后 broker 未保持 running/healthy"
  echo "RELOAD=PASS（SIGHUP 原地 reload；未重启 broker）"
}

# ---- 只读校验（文件存在/权限/ACL 模板）----
verify_only() {
  local errors=0
  [ -f "$VEHICLE_DIR/mqtt_password" ] || { echo "ERROR 缺少 $VEHICLE_DIR/mqtt_password"; errors=1; }
  [ -f "$VEHICLE_DIR/srt_publish_token" ] || { echo "ERROR 缺少 $VEHICLE_DIR/srt_publish_token"; errors=1; }
  [ -f "$VEHICLE_DIR/srt_publish_passphrase" ] || { echo "ERROR 缺少 $VEHICLE_DIR/srt_publish_passphrase"; errors=1; }
  grep -q "^$VEHICLE_ID:" "$RUNTIME_ROOT/secrets/mosquitto/passwords" \
    || { echo "ERROR passwords 未包含用户 $VEHICLE_ID"; errors=1; }
  grep -q "pattern write robot/%u/command_ack" "$RUNTIME_ROOT/secrets/mosquitto/acl" \
    || { echo "ERROR acl 缺少 pattern 模板"; errors=1; }
  for f in "$VEHICLE_DIR"/mqtt_password "$VEHICLE_DIR"/srt_publish_token "$VEHICLE_DIR"/srt_publish_passphrase; do
    [ -f "$f" ] && { mode="$(stat -c '%a' "$f")"; case "$mode" in 600|400) : ;; *) echo "ERROR 权限异常 $f ($mode)"; errors=1 ;; esac; }
  done
  [ "$errors" -eq 0 ] && echo "ONBOARD_VERIFY=PASS vehicle=$VEHICLE_ID" || return 1
}

# ---- broker 行为级 live ACL verify（61-verify-vehicle-acl.py，容器内执行）----
live_acl_verify() {
  detect_docker
  "${DOCKER[@]}" run --rm --network host \
    -v "$REPO_ROOT:/workspace:ro" \
    -v "$RUNTIME_ROOT:/runtime:ro" \
    "$ACL_IMAGE" python3 deploy/u-dao/scripts/61-verify-vehicle-acl.py \
    --vehicle "$VEHICLE_ID" --mqtt-host 127.0.0.1 --mqtt-port 18883 \
    || fail "live ACL verify 失败（VEHICLE_ACL_LIVE）"
}

# ---- SRT 凭据重派生（P0-B：master token/passphrase 旋转后同步单车副本；MQTT 不动）----
resync_srt() {
  [ -d "$VEHICLE_DIR" ] || fail "$VEHICLE_ID 凭据目录不存在（先执行默认生成）"
  MASTER_TOKEN="$(cat "$RUNTIME_ROOT/secrets/app/media_publish_token")"
  SRT_TOKEN="$(printf 'vehicle-srt:%s' "$VEHICLE_ID" | \
    openssl dgst -sha256 -hmac "$MASTER_TOKEN" | awk '{print $2}')"
  printf '%s' "$SRT_TOKEN" > "$VEHICLE_DIR/srt_publish_token"
  cp "$RUNTIME_ROOT/secrets/app/srt_publish_passphrase" "$VEHICLE_DIR/srt_publish_passphrase"
  chmod 600 "$VEHICLE_DIR/srt_publish_token" "$VEHICLE_DIR/srt_publish_passphrase"
  echo "ONBOARD_RESYNC_SRT=PASS vehicle=$VEHICLE_ID（仅重派生 SRT token/passphrase 副本；MQTT 凭据未动）"
}

# ---- 激活：password_file 更新 → 卷同步 → SIGHUP → verify → live ACL verify ----
activate() {
  update_password_file
  sync_config_volume
  reload_broker
  verify_only
  live_acl_verify
  echo "ONBOARD_APPLY=PASS vehicle=$VEHICLE_ID"
}

case "$MODE" in
  --verify) verify_only ;;
  --resync-srt) resync_srt ;;
  --apply)
    if credentials_exist; then
      echo "REUSE（凭据已存在，不旋转；直接激活）"
    else
      generate_credentials
      echo "GENERATED（首次生成）"
    fi
    activate ;;
  --force)
    echo "WARN 将旋转 $VEHICLE_ID 凭据：旧车端 MQTT credential 会失效（值不回显）"
    generate_credentials
    activate
    echo "ONBOARD_ROTATE=PASS vehicle=$VEHICLE_ID" ;;
  *)
    if credentials_exist; then
      fail "$VEHICLE_ID 凭据已存在（使用 --apply 激活，或 --force 旋转）"
    fi
    generate_credentials
    update_password_file
    echo "ONBOARD=PASS vehicle=$VEHICLE_ID（仅生成，未激活；broker 生效需 --apply）"
    echo "  files=$VEHICLE_DIR/{mqtt_password,srt_publish_token,srt_publish_passphrase,README.txt}" ;;
esac
