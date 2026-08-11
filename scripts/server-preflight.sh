#!/usr/bin/env sh
set -eu

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
SECRETS_DIR="${SECRETS_DIR:-secrets}"
BACKUP_PATH="${BACKUP_PATH:-backups}"
errors=0

fail() { printf 'ERROR %s\n' "$1" >&2; errors=$((errors + 1)); }
for command in docker git awk grep sed; do command -v "$command" >/dev/null 2>&1 || fail "missing command: $command"; done
docker info >/dev/null 2>&1 || fail 'Docker Engine unavailable'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose plugin unavailable'
[ -f "$ENV_FILE" ] || fail "missing SERVER env: $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid' "$ENV_FILE" && fail 'SERVER env contains placeholder' || true
  grep -Eq '^ENABLE_API_DOCS=false$' "$ENV_FILE" || fail 'ENABLE_API_DOCS=false is required'
fi

for relative in \
  app/database_url app/redis_url app/jwt_secret app/refresh_secret app/csrf_secret \
  app/bootstrap_admin_password app/mqtt_platform_password app/media_publish_token \
  postgres/password redis/password mosquitto/certs/ca.crt mosquitto/certs/server.crt \
  mosquitto/certs/server.key mosquitto/passwords mosquitto/acl nginx/server.crt nginx/server.key; do
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

if command -v timedatectl >/dev/null 2>&1; then
  timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes || fail 'time sync is not confirmed'
elif ! command -v chronyc >/dev/null 2>&1; then
  fail 'neither timedatectl nor chronyc can confirm time sync'
fi

if command -v ss >/dev/null 2>&1 && [ "${ALLOW_OCCUPIED_PORTS:-false}" != true ]; then
  for port in 80 443 8883; do ss -lnt | awk '{print $4}' | grep -Eq "[:.]$port$" && fail "port occupied: $port" || true; done
fi
docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml config --quiet || fail 'SERVER compose config failed'

[ "$errors" -eq 0 ] || { printf 'SERVER_PREFLIGHT=FAIL count=%s\n' "$errors" >&2; exit 1; }
printf 'SERVER_PREFLIGHT=PASS cpu=%s memory_kib=%s disk_free_kib=%s\n' "$cpu" "$memory_kib" "$disk_kib"
