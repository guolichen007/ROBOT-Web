#!/usr/bin/env bash
# Never source an env file as shell code, or echo inspect/ACL/secret output.
v1_required() {
  local config_required=false db_required cid
  if [ "${VEHICLE_AGENT_V1_ROLLOUT:-false}" = true ] ||
     [ "${VEHICLE_AGENT_V1_DOWNLINK_ENABLED:-false}" = true ] ||
     [[ ",${COMPOSE_PROFILES:-}," == *,vehicle-agent,* ]] ||
     { tr -d "\"' \r" < "$ENV_FILE" | grep -Eiq '^VEHICLE_AGENT_V1_(ROLLOUT|DOWNLINK_ENABLED)=(true|1|yes|on)$'; }; then
    config_required=true
  fi
  cid="$("${COMPOSE[@]}" ps -q api 2>/dev/null)" || return 1
  if [ -n "$cid" ] && [ "$config_required" != true ]; then
    db_required="$("${COMPOSE[@]}" exec -T api python -c '
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
with SessionLocal() as db:
    required = get_settings().vehicle_agent_v1_downlink_enabled or bool(db.scalar(select(Robot.id).where(Robot.enabled.is_(True), Robot.agent_protocol == "VEHICLE_AGENT_V1").limit(1)))
print("true" if required else "false")' 2>/dev/null)" || return 1
    case "$db_required" in true) config_required=true ;; false) : ;; *) return 1 ;; esac
  fi
  printf '%s\n' "$config_required"
}

v1_acl_gate() {
  # Reject an unexpected active config rather than validating an unused file.
  "${COMPOSE[@]}" exec -T mosquitto sh -c \
    'grep -Eq "^acl_file[[:space:]]+/mosquitto/secure/acl[[:space:]]*$" /mosquitto/config/mosquitto.conf' >/dev/null 2>&1 || return 1
  "${COMPOSE[@]}" exec -T mosquitto cat /mosquitto/secure/acl 2>/dev/null |
    python3 scripts/check_vehicle_agent_acl.py -
}
