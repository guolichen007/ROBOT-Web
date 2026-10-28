#!/usr/bin/env bash
# edge-verify.sh — firebotctl vehicle edge-verify 的受控实现。
#
# 验证当前 release：manifest source_sha == FINAL_SHA、layout、关键文件、权限模型、
# service inactive（安装≠启动）、runtime user 配置、bridge.env 与 CA 就绪。
# 只读；不输出任何 secret value。
#
# 用法：FIREBOT_REQUIRE_SHA=<FINAL_SHA> edge-verify.sh [--edge-home …]
set -euo pipefail

REQUIRE_SHA="${FIREBOT_REQUIRE_SHA:-}"
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
while [ $# -gt 0 ]; do
  case "$1" in
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
echo "$REQUIRE_SHA" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: FIREBOT_REQUIRE_SHA 必须 40 位 hex" >&2; exit 1; }

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }

# current 指向 releases/<FINAL_SHA>
if [ -L "$EDGE_HOME/current" ] && [ "$(readlink "$EDGE_HOME/current")" = "releases/$REQUIRE_SHA" ]; then
  pass "EDGE_CURRENT_SHA=yes"
else
  fail "EDGE_CURRENT_SHA=no（current=$(readlink "$EDGE_HOME/current" 2>/dev/null || echo 无)）"
fi

# manifest source_sha
manifest="$EDGE_HOME/current/manifest.json"
if grep -q "\"source_sha\": \"$REQUIRE_SHA\"" "$manifest" 2>/dev/null; then
  pass "EDGE_MANIFEST=yes"
else
  fail "EDGE_MANIFEST=no"
fi

# 关键文件
for f in VERSION bin/firebot-edge bridge/run_bridge.sh bridge/firebot_bridge/__init__.py lib/edge_paths.py monitor/health.py; do
  [ -e "$EDGE_HOME/current/$f" ] && pass "EDGE_FILE_$f=yes" || fail "EDGE_FILE_$f=no"
done

# 权限模型（fail-closed）
perm() {
  local path="$1" expect="$2"
  local actual
  actual="$(stat -c %a "$path" 2>/dev/null || echo MISSING)"
  [ "$actual" = "$expect" ]
}
perm "$EDGE_HOME/secrets" 700 && pass "EDGE_PERM_secrets=700" || fail "EDGE_PERM_secrets=700"
perm "$EDGE_HOME/backups" 700 && pass "EDGE_PERM_backups=700" || fail "EDGE_PERM_backups=700"
perm "$EDGE_HOME/runtime" 750 && pass "EDGE_PERM_runtime=750" || fail "EDGE_PERM_runtime=750"
perm "$EDGE_HOME/config/install.env" 644 && pass "EDGE_PERM_install.env=644" || fail "EDGE_PERM_install.env=644"

# runtime user 配置（禁止自动猜）
grep -qE '^FIREBOT_EDGE_USER=[a-z_][a-z0-9_-]*$' "$EDGE_HOME/config/install.env" 2>/dev/null \
  && pass "EDGE_RUNTIME_USER=configured" || fail "EDGE_RUNTIME_USER=not-configured"

# 安装≠启动：service inactive + disabled
edge_state="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
edge_enabled="$("$SYSTEMCTL" is-enabled firebot-vehicle-edge 2>/dev/null || echo disabled)"
[ "$edge_state" = "inactive" ] && pass "EDGE_SERVICE_INACTIVE=yes" || fail "EDGE_SERVICE_INACTIVE=no（$edge_state）"
[ "$edge_enabled" != "enabled" ] && pass "EDGE_SERVICE_DISABLED=yes" || fail "EDGE_SERVICE_DISABLED=no"

# config/secrets/certs 就绪
[ -f "$EDGE_HOME/config/identity.env" ] && pass "EDGE_IDENTITY=yes" || fail "EDGE_IDENTITY=no"
[ -f "$EDGE_HOME/config/bridge.env" ] && pass "EDGE_BRIDGE_ENV=yes" || fail "EDGE_BRIDGE_ENV=no"
[ -f "$EDGE_HOME/secrets/mqtt.env" ] && pass "EDGE_MQTT_ENV=yes" || fail "EDGE_MQTT_ENV=no"
[ -f "$EDGE_HOME/certs/mqtt-ca.crt" ] && pass "EDGE_MQTT_CA=yes" || fail "EDGE_MQTT_CA=no"

if [ "$FAILED" -ne 0 ]; then
  echo "EDGE_VERIFY=FAIL"
  exit 1
fi
echo "EDGE_VERIFY=PASS sha=$REQUIRE_SHA"
