#!/usr/bin/env bash
# Safe start gate. Never reads secret contents, never modifies env.
set -uo pipefail

UNIT="firebot-bridge"
ENV_FILE="/etc/firebot/bridge.env"
SECRET_FILE="/etc/firebot/bridge-secret.env"
CA_FILE="/etc/firebot/production-ca.crt"
UNIT_FILE="/etc/systemd/system/${UNIT}.service"

fail() { echo "STOP: $1" >&2; exit 1; }

[ -f "$ENV_FILE" ]    || fail "missing $ENV_FILE"
[ -f "$SECRET_FILE" ] || fail "missing $SECRET_FILE (existence only; content not read)"
[ -f "$CA_FILE" ]     || fail "missing $CA_FILE"
[ -f "$UNIT_FILE" ]   || fail "missing $UNIT_FILE"

# Source only the safe-flag variables we gate on (never the secret file).
set -a
. "$ENV_FILE"
set +a

[ "${BRIDGE_STUB_MODE:-false}" = "false" ]        || fail "BRIDGE_STUB_MODE must be false"
[ -z "${FIREBOT_SUPPORTED_COMMANDS:-}" ]           || fail "FIREBOT_SUPPORTED_COMMANDS must be empty"
[ -z "${FIREBOT_SENSORS:-}" ]                      || fail "FIREBOT_SENSORS must be empty"
[ "${FIREBOT_LOCATION_ENABLED:-false}" = "false" ] || fail "FIREBOT_LOCATION_ENABLED must be false"

grep -q 'firebot_bridge.main' "$UNIT_FILE" \
  || fail "unit ExecStart must run python3 -m firebot_bridge.main"
grep -q 'ROS_MASTER_URI=http://127.0.0.1:1' "$UNIT_FILE" \
  || fail "unit must set ROS_MASTER_URI=http://127.0.0.1:1"

if [ "$(systemctl is-active "$UNIT" 2>/dev/null || echo inactive)" = "active" ]; then
  echo "PASS: $UNIT already running"
  exit 0
fi

systemctl start "$UNIT"
echo "STARTED=$UNIT"
