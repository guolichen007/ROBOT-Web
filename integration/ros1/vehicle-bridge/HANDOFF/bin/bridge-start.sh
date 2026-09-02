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
# 代码已实现但尚未现场验收的命令白名单；只拒绝「代码未实现」的声明，
# 不把「supported_commands 必须为空」当作永久合同（未来逐命令开放时在此扩展）。
IMPLEMENTED_CMDS="patrol,stop_motion"
declared_supported="$(env_value FIREBOT_SUPPORTED_COMMANDS)"
if [ -n "$declared_supported" ]; then
  for c in $(echo "$declared_supported" | tr ',' ' '); do
    case ",$IMPLEMENTED_CMDS," in
      *",$c,"*) ;;
      *) fail "FIREBOT_SUPPORTED_COMMANDS 声明了未实现的命令: $c" ;;
    esac
  done
  echo "  注意：FIREBOT_SUPPORTED_COMMANDS=$declared_supported（控制尚未现场验收，须人工确认与 approved capability 一致）"
else
  echo "  FIREBOT_SUPPORTED_COMMANDS 为空（控制未开放，符合当前 approved capability）"
fi
[ -z "$(env_value FIREBOT_SENSORS)" ] \
  || fail "FIREBOT_SENSORS must be empty"
[ "$(env_value FIREBOT_LOCATION_ENABLED)" = "false" ] \
  || fail "FIREBOT_LOCATION_ENABLED must be false"

# ---- effective systemd unit (systemctl show, not raw base unit file) ----
show_val() { systemctl show "$UNIT" -p "$1" --value 2>/dev/null || true; }

EFF_USER="$(show_val User)"
EFF_EXEC="$(show_val ExecStart)"

[ "$EFF_USER" = "tl" ] \
  || fail "effective User must be tl (got: $EFF_USER)"
# 当前 /opt 正式安装架构：ExecStart=/bin/bash /opt/firebot/vehicle-bridge/run_bridge.sh <env>。
# 不再要求旧形态 WorkingDirectory=/home/tl/vehicle-bridge 或 python3 -m firebot_bridge.main。
case "$EFF_EXEC" in
  *"/run_bridge.sh"*) ;;
  *) fail "effective ExecStart must reference run_bridge.sh (got: $EFF_EXEC)" ;;
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
