#!/usr/bin/env bash
# readonly-preflight.sh — 兼容 shim（VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION / 规格三十九）。
# 生产逻辑唯一 source of truth：vehicle-edge/lifecycle/readonly-preflight.sh。
# 本文件仅 thin wrapper，禁止重复业务逻辑（NO_DUPLICATED_PRODUCTION_LOGIC）。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SELF_DIR/vehicle-edge/lifecycle/readonly-preflight.sh" "$@"
