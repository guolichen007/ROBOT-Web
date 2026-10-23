#!/usr/bin/env bash
# Detector counterexamples; no Docker daemon, broker, credentials or production access.
set -euo pipefail
source scripts/vehicle-agent-rollout.sh
ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
COMPOSE=(compose_stub)
CID=''
DB_REQUIRED=false
DB_ERROR=false
compose_stub() {
  case "$1" in
    ps) printf '%s' "$CID" ;;
    exec) [ "$DB_ERROR" = false ] || return 1; printf '%s' "$DB_REQUIRED" ;;
    *) return 1 ;;
  esac
}
unset VEHICLE_AGENT_V1_ROLLOUT VEHICLE_AGENT_V1_DOWNLINK_ENABLED COMPOSE_PROFILES
printf 'VEHICLE_AGENT_V1_ROLLOUT=false\n' > "$ENV_FILE"
[ "$(v1_required)" = false ]
CID=api
DB_REQUIRED=true
[ "$(v1_required)" = true ]
DB_ERROR=true
if v1_required >/dev/null; then echo 'OPS_UNKNOWN_DB=FAIL'; exit 1; fi
printf 'VEHICLE_AGENT_V1_ROLLOUT="true"\n' > "$ENV_FILE"
[ "$(v1_required)" = true ]  # first deployment may still run pre-v1 API code
echo 'V1_ROLLOUT_DETECTOR=PASS canonical/DB-enabled/unknown-DB/first-rollout'
