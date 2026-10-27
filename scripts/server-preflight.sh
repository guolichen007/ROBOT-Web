#!/usr/bin/env bash
# Firebot SERVER preflight（legacy 现场 / U-Dao 云双模式）。
# U-Dao 模式：U_DAO_MODE=1 + FIREBOT_COMPOSE_FILES 含 deploy/u-dao/compose.u-dao.yml +
#             FIREBOT_RUNTIME_ROOT（secrets 绝对路径源）。
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
SECRETS_DIR="${SECRETS_DIR:-secrets}"
BACKUP_PATH="${BACKUP_PATH:-backups}"
U_DAO_MODE="${U_DAO_MODE:-0}"
errors=0
COMPOSE_FILES="${FIREBOT_COMPOSE_FILES:-docker-compose.server.yml}"
source scripts/docker_cmd.sh
docker_cmd_detect || fail 'Docker Engine unavailable（docker 与 sudo -n docker 均不可用）'
COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in $COMPOSE_FILES; do COMPOSE+=(-f "$f"); done
source scripts/vehicle-agent-rollout.sh

fail() { printf 'ERROR %s\n' "$1" >&2; errors=$((errors + 1)); }
for command in git awk grep sed python3; do command -v "$command" >/dev/null 2>&1 || fail "missing command: $command"; done
"${DOCKER[@]}" compose version >/dev/null 2>&1 || fail 'Docker Compose plugin unavailable'
[ -f "$ENV_FILE" ] || fail "missing SERVER env: $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid' "$ENV_FILE" && fail 'SERVER env contains placeholder' || true
  grep -Eq '^ENABLE_API_DOCS=false$' "$ENV_FILE" || fail 'ENABLE_API_DOCS=false is required'
fi

# ---- legacy fail-closed 恢复（R2-12）----
# 基座 compose 已将 ${...:?} 软化为 ${...:-127.0.0.1}（compose 按文件解析先于 override 合并所致），
# 旧现场"缺变量必须失败"的语义在此恢复：legacy 模式缺 SERVER_TAILSCALE_IP/SERVER_LAN_IP 或占位值 → FAIL。
if [ "$U_DAO_MODE" != 1 ]; then
  for var in SERVER_TAILSCALE_IP SERVER_LAN_IP; do
    value="$(grep -E "^[[:space:]]*$var=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d '[:space:]' || true)"
    if [ -z "$value" ] || [[ "$value" == 192.0.2.* ]]; then
      fail "legacy server requires explicit $var（fail-closed 语义，勿用占位地址）"
    fi
  done
fi

# ---- U-Dao 云模式断言 ----
if [ "$U_DAO_MODE" = 1 ]; then
  [ -n "${FIREBOT_RUNTIME_ROOT:-}" ] || fail 'U_DAO_MODE=1 requires FIREBOT_RUNTIME_ROOT'
  SECRETS_DIR="$FIREBOT_RUNTIME_ROOT/secrets"
  grep -Eq '^[[:space:]]*SERVER_TAILSCALE_IP=' "$ENV_FILE" && fail 'U-Dao env must not define SERVER_TAILSCALE_IP' || true
  grep -Eq '^[[:space:]]*SERVER_LAN_IP=' "$ENV_FILE" && fail 'U-Dao env must not define SERVER_LAN_IP' || true
  grep -Eq 'FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST' "$ENV_FILE" && fail 'U-Dao env must not contain legacy canary fields' || true
  case " $COMPOSE_FILES " in
    *deploy/u-dao/compose.u-dao.yml*) : ;;
    *) fail 'U_DAO_MODE=1 requires FIREBOT_COMPOSE_FILES to include deploy/u-dao/compose.u-dao.yml' ;;
  esac
fi

# ---- secrets 清单与权限 ----
if [ "$U_DAO_MODE" = 1 ]; then
  secret_list="app/database_url app/redis_url app/jwt_secret app/refresh_secret app/csrf_secret \
app/bootstrap_admin_password app/mqtt_platform_password app/media_publish_token app/srt_publish_passphrase \
postgres/password redis/password mosquitto/certs/ca.crt mosquitto/certs/server.crt mosquitto/certs/server.key \
mosquitto/passwords mosquitto/acl"
else
  secret_list="app/database_url app/redis_url app/jwt_secret app/refresh_secret app/csrf_secret \
app/bootstrap_admin_password app/mqtt_platform_password app/media_publish_token \
postgres/password redis/password mosquitto/certs/ca.crt mosquitto/certs/server.crt mosquitto/certs/server.key \
mosquitto/passwords mosquitto/acl nginx/server.crt nginx/server.key"
fi
for relative in $secret_list; do
  path="$SECRETS_DIR/$relative"
  [ -s "$path" ] || fail "missing or empty secret: $path"
  if [ -e "$path" ]; then
    mode="$(stat -c '%a' "$path" 2>/dev/null || printf '')"
    case "$mode" in 600|640|400|440) : ;; *) fail "secret permissions must be 600/640/400/440: $path ($mode)" ;; esac
  fi
done

mkdir -p "$BACKUP_PATH" || fail "cannot create backup path: $BACKUP_PATH"
probe="$BACKUP_PATH/.firebot-write-test-$$"
( umask 077 && printf ok > "$probe" && rm -f "$probe" ) || fail "backup path not writable: $BACKUP_PATH"

cpu="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 0)"
memory_kib="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || printf 0)"
disk_kib="$(df -Pk "$BACKUP_PATH" | awk 'NR==2 {print $4}')"
[ "$cpu" -ge 2 ] || fail "CPU below baseline check: $cpu"
[ "$memory_kib" -ge 4194304 ] || fail "RAM below baseline check: ${memory_kib}KiB"
[ "$disk_kib" -ge 20971520 ] || fail "disk free below baseline check: ${disk_kib}KiB"

# ---- 时间同步（R2-20：JWT/media ticket/命令过期/MQTT Expiry/TLS 全部依赖正确时钟）----
if command -v timedatectl >/dev/null 2>&1; then
  timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes || fail 'time sync is not confirmed'
else
  fail 'timedatectl unavailable; cannot confirm time sync'
fi
if command -v systemctl >/dev/null 2>&1; then
  ntp_ok=false
  for svc in ntp chrony systemd-timesyncd; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then ntp_ok=true; break; fi
  done
  [ "$ntp_ok" = true ] || fail 'no active NTP service (ntp/chrony/systemd-timesyncd)'
fi

firebot_owns_port() {
  port="$1"
  svc=""
  case "$port" in
    80|443) svc=nginx ;;
    8883) svc=mosquitto ;;
  esac
  [ -n "$svc" ] || return 1
  cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] && "${DOCKER[@]}" port "$cid" "$port" >/dev/null 2>&1
}

firebot_owns_cloud_port() {
  host_port="$1"
  svc=""
  target=""
  case "$host_port" in
    18883) svc=mosquitto; target=8883 ;;
    18554) svc=mediamtx; target=8554 ;;
    18080) svc=nginx; target=80 ;;
    8189) svc=mediamtx; target=8189 ;;
  esac
  [ -n "$svc" ] || return 1
  cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] && "${DOCKER[@]}" port "$cid" "$target" >/dev/null 2>&1
}

# shared ingress（platform 资源）为 80/443 的合法 owner
ingress_owns_80_443() {
  "${DOCKER[@]}" compose -p u-dao-ingress ps -q 2>/dev/null | grep -q .
}

if command -v ss >/dev/null 2>&1 && [ "${ALLOW_OCCUPIED_PORTS:-false}" != true ]; then
  if [ "$U_DAO_MODE" = 1 ]; then
    # 云模式：Firebot 自身端口必须空闲；80/443 若被占用必须属于 u-dao-ingress
    for port in 18883 18554 18080 8189; do
      if ss -lnt | awk '{print $4}' | grep -Eq "[:.]$port$"; then
        if [ "${UPDATE_MODE:-false}" = true ] && firebot_owns_cloud_port "$port"; then
          : # 运行中的 Firebot 自有端口，升级放行
        else
          fail "port occupied: $port"
        fi
      fi
    done
    if ss -lnu | awk '{print $4}' | grep -Eq "[:.]18890$"; then
      [ "${UPDATE_MODE:-false}" = true ] || fail "port occupied: 18890/udp"
    fi
    for port in 80 443; do
      if ss -lnt | awk '{print $4}' | grep -Eq "[:.]$port$"; then
        ingress_owns_80_443 || fail "port $port occupied by non-ingress owner (U-Dao 80/443 归 shared ingress)"
      fi
    done
  else
    for port in 80 443 8883; do
      if ss -lnt | awk '{print $4}' | grep -Eq "[:.]$port$"; then
        if [ "${UPDATE_MODE:-false}" = true ] && firebot_owns_port "$port"; then
          : # 运行中的 Firebot 自有端口，升级放行
        else
          fail "port occupied: $port"
        fi
      fi
    done
  fi
fi
"${COMPOSE[@]}" config --quiet || fail 'SERVER compose config failed'

V1_REQUIRED="$(v1_required)" || { fail 'cannot determine v1 rollout requirement'; V1_REQUIRED=true; }
if [ "$V1_REQUIRED" = true ]; then
  python3 scripts/check_vehicle_agent_acl.py "$SECRETS_DIR/mosquitto/acl" || fail 'v1 source ACL missing required permissions'
  if [ -n "$("${COMPOSE[@]}" ps -q mosquitto 2>/dev/null)" ]; then
    v1_acl_gate || fail 'v1 active broker ACL missing required permissions'
  fi
fi

[ "$errors" -eq 0 ] || { printf 'SERVER_PREFLIGHT=FAIL count=%s\n' "$errors" >&2; exit 1; }
printf 'SERVER_PREFLIGHT=PASS cpu=%s memory_kib=%s disk_free_kib=%s\n' "$cpu" "$memory_kib" "$disk_kib"
