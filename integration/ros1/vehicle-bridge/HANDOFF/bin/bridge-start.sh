#!/usr/bin/env bash
# Safe start gate. Fail-closed: never sources bridge.env, never reads secrets,
# verifies effective systemd unit, and only reports success after active + MQTT connected.
set -euo pipefail

UNIT="firebot-bridge"
ENV_FILE="/etc/firebot/bridge.env"
SECRET_FILE="/etc/firebot/bridge-secret.env"
CA_FILE="/etc/firebot/production-ca.crt"
STATUS_FILE="/run/firebot-bridge/status.json"

fail() { echo "STOP: $1" >&2; exit 1; }

# ---- file existence (existence only; secret content never read) ----
[ -f "$ENV_FILE" ]    || fail "missing $ENV_FILE"
[ -f "$SECRET_FILE" ] || fail "missing $SECRET_FILE (existence only)"
[ -f "$CA_FILE" ]     || fail "missing $CA_FILE"

# ---- parse exactly 4 whitelist keys; never source the env file ----
env_value() {
  local key="$1" val=""
  val="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 \
         | sed -E 's/^[[:space:]]*[A-Za-z_]+=//; s/[[:space:]]*#.*$//; s/[[:space:]]+$//; s/^"//; s/"$//' || true)"
  printf '%s' "$val"
}

[ "$(env_value BRIDGE_STUB_MODE)" = "false" ] \
  || fail "BRIDGE_STUB_MODE must be false"
[ -z "$(env_value FIREBOT_SUPPORTED_COMMANDS)" ] \
  || fail "FIREBOT_SUPPORTED_COMMANDS must be empty"
[ -z "$(env_value FIREBOT_SENSORS)" ] \
  || fail "FIREBOT_SENSORS must be empty"
[ "$(env_value FIREBOT_LOCATION_ENABLED)" = "false" ] \
  || fail "FIREBOT_LOCATION_ENABLED must be false"

# ---- effective systemd unit (systemctl show, not raw base unit file) ----
show_val() { systemctl show "$UNIT" -p "$1" --value 2>/dev/null || true; }

EFF_USER="$(show_val User)"
EFF_WD="$(show_val WorkingDirectory)"
EFF_EXEC="$(show_val ExecStart)"
EFF_ENV="$(show_val Environment)"

[ "$EFF_USER" = "tl" ] \
  || fail "effective User must be tl (got: $EFF_USER)"
[ "$EFF_WD" = "/home/tl/vehicle-bridge" ] \
  || fail "effective WorkingDirectory must be /home/tl/vehicle-bridge (got: $EFF_WD)"
case "$EFF_EXEC" in
  *python3*firebot_bridge.main*) ;;
  *) fail "effective ExecStart must run python3 -m firebot_bridge.main (got: $EFF_EXEC)" ;;
esac
case "$EFF_ENV" in
  *ROS_MASTER_URI=http://127.0.0.1:1*) ;;
  *) fail "effective Environment must contain ROS_MASTER_URI=http://127.0.0.1:1" ;;
esac

# ---- status.json helpers ----
mqtt_is_true() {
  [ -f "$STATUS_FILE" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if d.get("mqtt_connected") is True else 1)' "$STATUS_FILE" 2>/dev/null
}

is_active() { [ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" = "active" ]; }

# ---- already running ----
if is_active; then
  if mqtt_is_true; then
    echo "BRIDGE_ALREADY_RUNNING=PASS"
    exit 0
  fi
  echo "BRIDGE_ALREADY_RUNNING=FAIL (active but mqtt_connected != true)" >&2
  exit 1
fi

# ---- start + wait (<=10s) ----
if ! systemctl start "$UNIT"; then
  echo "BRIDGE_START=FAIL (systemctl start failed)" >&2
  exit 1
fi

for _ in $(seq 1 20); do
  if is_active; then
    MAINPID="$(show_val MainPID)"
    if [ -n "${MAINPID:-}" ] && [ "${MAINPID}" != "0" ] && mqtt_is_true; then
      echo "BRIDGE_START=PASS"
      exit 0
    fi
  fi
  sleep 0.5
done

echo "BRIDGE_START=FAIL (not active+mqtt_connected within 10s)" >&2
exit 1
