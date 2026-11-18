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
#   bash deploy/u-dao/scripts/60-onboard-vehicle.sh firebot-vehicle-01 --media-only
#     （G4-R1：只交付媒体凭证——派生/校准 srt_publish_token + passphrase 副本（0600）；
#       不生成/不旋转 MQTT 密码、不改 password_file、不调用 docker、不 reload 任何 Broker）
#
# 主密钥（secrets/app/media_publish_token）在任何分支都只由 python 进程内读取文件，
# 绝不进入任何进程 argv / 环境变量 / shell 变量（G4-R1 D：--resync-srt 旧实现的
# `openssl dgst -hmac "$MASTER_TOKEN"` 已移除）。
set -euo pipefail

VEHICLE_ID="${1:-}"
MODE="${2:-}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
MOSQ_IMG="eclipse-mosquitto:2.1.2-alpine"
ACL_IMAGE="${ACL_CHECK_IMAGE:-firebot-python-test:u-dao}"
VEHICLE_DIR="$RUNTIME_ROOT/secrets/vehicles/$VEHICLE_ID"

fail() { echo "ONBOARD=FAIL: $1" >&2; exit 1; }

[ -n "$VEHICLE_ID" ] || fail "用法: $0 <VEHICLE_ID> [--force|--verify|--apply|--resync-srt|--media-only]"
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

# ---- per-vehicle SRT token 派生（唯一入口；--apply/--force/--resync-srt/--media-only 共用）----
# G1-GO0 A / G4-R1 D：master token 绝不经 argv（openssl -hmac "$KEY" 会把密钥暴露在进程
# 参数中，/proc/<pid>/cmdline 与 ps 对同机任意用户可见）。改由 python 进程内读取密钥文件
# 计算 HMAC——argv 只有代码与文件路径；消息经 stdin；结果只经 stdout 返回。
# 密钥读取语义必须与 API 端 Settings._read_secret 完全一致（UTF-8 + strip）：
# 02-gen-bootstrap-secrets.sh 以 `openssl rand -base64 N >` 落盘，文件末尾带换行；
# 若按原始字节作 HMAC 密钥，派生 token 与 authorize_publish 期望值不一致（401）。
derive_srt_token() {
  [ -s "$RUNTIME_ROOT/secrets/app/media_publish_token" ] || fail "master media_publish_token 缺失或为空"
  printf 'vehicle-srt:%s' "$VEHICLE_ID" | python3 -c '
import hashlib
import hmac
import sys

key = open(sys.argv[1], encoding="utf-8").read().strip().encode("utf-8")
if not key:
    sys.exit("empty master key")
digest = hmac.new(key, sys.stdin.buffer.read(), hashlib.sha256).hexdigest()
print(digest)
' "$RUNTIME_ROOT/secrets/app/media_publish_token"
}

# ---- 写入媒体凭证（token + 平台共享 passphrase 副本）：先 0600 临时文件再原子 rename ----
write_media_credentials() {
  local token tmp
  umask 077
  [ -s "$RUNTIME_ROOT/secrets/app/srt_publish_passphrase" ] || fail "master srt_publish_passphrase 缺失或为空"
  mkdir -p "$VEHICLE_DIR"
  chmod 700 "$VEHICLE_DIR"
  token="$(derive_srt_token)" || fail "SRT token 派生失败"
  echo "$token" | grep -qE '^[0-9a-f]{64}$' || fail "SRT token 派生结果格式异常"
  tmp="$(mktemp "$VEHICLE_DIR/.srt_publish_token.XXXXXX")"
  printf '%s' "$token" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$VEHICLE_DIR/srt_publish_token"
  tmp="$(mktemp "$VEHICLE_DIR/.srt_publish_passphrase.XXXXXX")"
  cat "$RUNTIME_ROOT/secrets/app/srt_publish_passphrase" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$VEHICLE_DIR/srt_publish_passphrase"
}

# ---- 生成凭据（真值不入 Git、不回显）----
generate_credentials() {
  umask 077
  mkdir -p "$VEHICLE_DIR"
  chmod 700 "$VEHICLE_DIR"
  MQTT_PW="$(openssl rand -base64 32)"
  printf '%s' "$MQTT_PW" > "$VEHICLE_DIR/mqtt_password"
  write_media_credentials
  chmod 600 "$VEHICLE_DIR/mqtt_password"
  {
    echo "# $VEHICLE_ID 凭据交付说明"
    echo "mqtt_password        = $VEHICLE_ID 的 MQTT 密码（MQTT username == vehicle_id）"
    echo "srt_publish_token    = per-vehicle SRT 发布 token（HMAC 派生，身份/发布授权隔离）"
    echo "srt_publish_passphrase = PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE（平台共享链路加密，非 per-vehicle）"
  } > "$VEHICLE_DIR/README.txt"
  chmod 600 "$VEHICLE_DIR/README.txt"
}

# ---- 更新 password_file（G1-GO0 A：密码经 stdin 交互式输入，绝不经 argv）----
# 旧实现 `mosquitto_passwd -b ... "$(cat ...)"` 会把明文密码暴露在 docker run 参数、
# 容器 COMMAND 与 docker inspect 记录中。新实现：密码文件内容经管道两次送入
# mosquitto_passwd 的交互式 stdin（Password:/Reenter password:），进程 argv 全程
# 无密码；stderr 落盘（仅提示语，不含密码）供排障，失败即 fail 不回显。
update_password_file() {
  detect_docker
  local errfile
  errfile="$(mktemp /tmp/firebot-onboard-passwd.XXXXXX)"
  chmod 600 "$errfile"
  if ! { cat "$VEHICLE_DIR/mqtt_password"; echo; cat "$VEHICLE_DIR/mqtt_password"; echo; } \
    | "${DOCKER[@]}" run --rm -i -v "$RUNTIME_ROOT/secrets/mosquitto:/work:rw" "$MOSQ_IMG" \
      mosquitto_passwd /work/passwords "$VEHICLE_ID" 2>"$errfile"; then
    fail "mosquitto_passwd 更新失败（诊断见 ${errfile}，不含密码）"
  fi
  rm -f "$errfile"
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
  [ -d "$VEHICLE_DIR" ] || fail "$VEHICLE_ID 凭据目录不存在（先执行默认生成或 --media-only）"
  write_media_credentials
  echo "ONBOARD_RESYNC_SRT=PASS vehicle=$VEHICLE_ID（仅重派生 SRT token/passphrase 副本；MQTT 凭据未动）"
}

# ---- 只交付媒体凭证（G4-R1 D：001 首次接入只需视频发布）----
# 幂等：token 为确定性派生，重复执行结果相同；不创建/不读取/不改动 mqtt_password、
# password_file、mosquitto 配置卷，不调用 docker / sudo，不 reload 任何 Broker。
media_only() {
  local state="CREATED"
  [ -f "$VEHICLE_DIR/srt_publish_token" ] && state="REUSED"
  write_media_credentials
  if [ ! -f "$VEHICLE_DIR/README.txt" ]; then
    {
      echo "# $VEHICLE_ID 媒体凭证交付说明（--media-only；不含 MQTT 凭据）"
      echo "srt_publish_token    = per-vehicle SRT 发布 token（HMAC 派生，身份/发布授权隔离）"
      echo "srt_publish_passphrase = PLATFORM_SHARED_SRT_ENCRYPTION_PASSPHRASE（平台共享链路加密，非 per-vehicle）"
    } > "$VEHICLE_DIR/README.txt"
    chmod 600 "$VEHICLE_DIR/README.txt"
  fi
  echo "ONBOARD_MEDIA_ONLY=PASS vehicle=$VEHICLE_ID state=$state（仅媒体凭证；MQTT 未生成/未轮换；Broker 未 reload）"
  echo "  files=$VEHICLE_DIR/{srt_publish_token,srt_publish_passphrase}"
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
  --media-only) media_only ;;
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
  "")
    if credentials_exist; then
      fail "$VEHICLE_ID 凭据已存在（使用 --apply 激活，或 --force 旋转）"
    fi
    generate_credentials
    update_password_file
    echo "ONBOARD=PASS vehicle=$VEHICLE_ID（仅生成，未激活；broker 生效需 --apply）"
    echo "  files=$VEHICLE_DIR/{mqtt_password,srt_publish_token,srt_publish_passphrase,README.txt}" ;;
  *) fail "未知模式: $MODE（可选 --apply|--force|--verify|--resync-srt|--media-only）" ;;
esac
