#!/usr/bin/env bash
# U-Dao 平台侧单车凭据生成（语义对应 firebotctl vehicle onboard；车端 firebotctl 流程不变）。
# 生成（全部只写 runtime，不入 Git，不回显）：
#   R<id> MQTT username（== vehicle_id，与 acl.example 的 pattern 约定一致）
#   R<id> MQTT password（追加到 runtime/secrets/mosquitto/passwords）
#   R<id> SRT publish credential（token）与独立加密 passphrase
# ACL 无需修改：acl.example 的 `pattern %u` 自动按 username 隔离（只写自己遥测、只读自己命令）。
# 用法：
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R001            # 生成凭据（幂等保护）
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R001 --force    # 重建凭据（旧密码失效）
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R001 --verify   # 只读校验
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh R001 --apply    # 生成 + reload mosquitto（激活）
set -euo pipefail

VEHICLE_ID="${1:-}"
MODE="${2:-}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
MOSQ_IMG="eclipse-mosquitto:2.1.2-alpine"
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

# ---- 只读校验 ----
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

# ---- 生成 ----
onboard() {
  local force=false
  [ "$MODE" = "--force" ] && force=true
  if [ -d "$VEHICLE_DIR" ] && [ "$force" != true ]; then
    fail "$VEHICLE_ID 凭据已存在（--force 重建会使旧密码失效）"
  fi
  detect_docker
  umask 077
  mkdir -p "$VEHICLE_DIR"
  chmod 700 "$VEHICLE_DIR"

  # MQTT password（车辆用户名 == vehicle_id）
  MQTT_PW="$(openssl rand -base64 32)"
  printf '%s' "$MQTT_PW" > "$VEHICLE_DIR/mqtt_password"
  chmod 600 "$VEHICLE_DIR/mqtt_password"

  # per-vehicle SRT 发布 token：HMAC-SHA256(master, "vehicle-srt:"+vehicle_id) 确定性派生
  # （与 API authorize_media 同算法；不存库、泄露无法推他车 token）
  MASTER_TOKEN="$(cat "$RUNTIME_ROOT/secrets/app/media_publish_token")"
  SRT_TOKEN="$(printf 'vehicle-srt:%s' "$VEHICLE_ID" | \
    openssl dgst -sha256 -hmac "$MASTER_TOKEN" | awk '{print $2}')"
  printf '%s' "$SRT_TOKEN" > "$VEHICLE_DIR/srt_publish_token"
  # SRT 链路加密 passphrase：平台共享（PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE），
  # 非 per-vehicle；身份隔离由 per-vehicle token 承担，链路加密由平台统一密钥承担。
  cp "$RUNTIME_ROOT/secrets/app/srt_publish_passphrase" "$VEHICLE_DIR/srt_publish_passphrase"
  chmod 600 "$VEHICLE_DIR/srt_publish_token" "$VEHICLE_DIR/srt_publish_passphrase"

  # passwords 文件更新（mosquitto_passwd 追加；容器内 root 产物 chown 回 ubuntu）
  if [ "$force" = true ]; then
    "${DOCKER[@]}" run --rm -v "$RUNTIME_ROOT/secrets/mosquitto:/work:rw" "$MOSQ_IMG" \
      sh -c "mosquitto_passwd -b /work/passwords '$VEHICLE_ID' '$MQTT_PW'" 2>/dev/null
  else
    "${DOCKER[@]}" run --rm -v "$RUNTIME_ROOT/secrets/mosquitto:/work:rw" "$MOSQ_IMG" \
      mosquitto_passwd -b /work/passwords "$VEHICLE_ID" "$MQTT_PW" 2>/dev/null
  fi
  sudo -n chown ubuntu:ubuntu "$RUNTIME_ROOT/secrets/mosquitto/passwords" || fail "chown passwords 失败"
  chmod 640 "$RUNTIME_ROOT/secrets/mosquitto/passwords"

  # 凭据语义说明（交付文档，不含真值）
  {
    echo "# $VEHICLE_ID 凭据交付说明"
    echo "mqtt_password        = $VEHICLE_ID 的 MQTT 密码（MQTT username == vehicle_id）"
    echo "srt_publish_token    = per-vehicle SRT 发布 token（HMAC 派生，身份/发布授权隔离）"
    echo "srt_publish_passphrase = PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE（平台共享链路加密，非 per-vehicle）"
  } > "$VEHICLE_DIR/README.txt"
  chmod 600 "$VEHICLE_DIR/README.txt"
  echo "ONBOARD=PASS vehicle=$VEHICLE_ID"
  echo "  mqtt_username=$VEHICLE_ID（值不回显）"
  echo "  files=$VEHICLE_DIR/{mqtt_password,srt_publish_token,srt_publish_passphrase,README.txt}"
  echo "  note=ACL 由 acl.example 的 pattern %u 自动隔离；broker 生效需 --apply（SIGHUP 原地 reload）"
}

# ---- 激活：对运行中的 u-dao-firebot mosquitto 容器发 SIGHUP 原地 reload（不重启、不断已有连接）----
reload_broker() {
  detect_docker
  # 通过 compose project/service labels 精确定位（禁止 hardcode 容器名）
  local cids
  cids="$("${DOCKER[@]}" ps -q -f "label=com.docker.compose.project=u-dao-firebot" \
           -f "label=com.docker.compose.service=mosquitto" 2>/dev/null || true)"
  local count
  count="$(printf '%s\n' "$cids" | grep -c .)"
  if [ "$count" -eq 0 ]; then
    fail "未找到 u-dao-firebot mosquitto 容器（0 个）"
  elif [ "$count" -gt 1 ]; then
    fail "找到 $count 个 mosquitto 容器（必须恰好 1 个）"
  fi
  "${DOCKER[@]}" kill --signal HUP "$cids" || fail "SIGHUP 发送失败"
  sleep 2
  "${DOCKER[@]}" inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cids" \
    | grep -qE '^true( healthy)?$' || fail "HUP 后 broker 未保持 running/healthy"
  echo "RELOAD=PASS（SIGHUP 原地 reload；未重启 broker）"
}

case "$MODE" in
  --verify) verify_only ;;
  --apply)
    onboard
    reload_broker
    verify_only ;;
  *) onboard ;;
esac
