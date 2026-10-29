# legacy_health.sh — 旧链路（legacy firebot-bridge）健康判定唯一实现。
#
# legacy_link_health 必须确认：
#   firebot-bridge LoadState=loaded + ActiveState=active
#   $LEGACY_BRIDGE_ENV（默认 /etc/firebot/bridge.env）FIREBOT_MQTT_HOST == 100.110.31.112（精确，非仅非空）
#   $LEGACY_BRIDGE_STATUS（默认 /run/firebot-bridge/status.json）mqtt_connected == true
#
# 生产禁止 /tmp fallback（历史 /tmp 状态不能当作在线证据）。
# 测试只能经 FIREBOT_TEST_BRIDGE_STATUS_FILE / FIREBOT_TEST_LEGACY_BRIDGE_ENV 显式注入 fixture。
#
# 依赖：systemd_state.sh（unit_show_all）。
# 使用：source 本文件（及 systemd_state.sh）后调用 legacy_link_health。

# 若调用方尚未 source systemd_state.sh，则从本文件同目录 source（幂等，只定义函数）。
if ! command -v unit_show_all >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  . "$(dirname "${BASH_SOURCE[0]}")/systemd_state.sh"
fi

LEGACY_BRIDGE_ENV="${FIREBOT_TEST_LEGACY_BRIDGE_ENV:-/etc/firebot/bridge.env}"
LEGACY_BRIDGE_STATUS="${FIREBOT_TEST_BRIDGE_STATUS_FILE:-/run/firebot-bridge/status.json}"
LEGACY_MQTT_HOST_EXPECTED="100.110.31.112"

legacy_mqtt_host() {
  grep -E '^FIREBOT_MQTT_HOST=' "$LEGACY_BRIDGE_ENV" 2>/dev/null \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}

legacy_mqtt_connected() {
  python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1],encoding="utf-8")).get("mqtt_connected") is True else "false")' \
    "$LEGACY_BRIDGE_STATUS" 2>/dev/null || echo "false"
}

legacy_link_health() {
  unit_show_all "firebot-bridge"
  local host connected
  host="$(legacy_mqtt_host)"
  connected="$(legacy_mqtt_connected)"
  if [ "$UNIT_SHOW_LOAD" = "loaded" ] && [ "$UNIT_SHOW_ACTIVE" = "active" ] \
     && [ "$host" = "$LEGACY_MQTT_HOST_EXPECTED" ] && [ "$connected" = "true" ]; then
    echo "LEGACY_LINK_HEALTH=PASS"
    echo "LEGACY_MQTT_HOST=$host"
    echo "LEGACY_MQTT_CONNECTED=true"
    return 0
  fi
  echo "LEGACY_LINK_HEALTH=FAIL"
  echo "LEGACY_LOAD_STATE=$UNIT_SHOW_LOAD"
  echo "LEGACY_ACTIVE_STATE=$UNIT_SHOW_ACTIVE"
  echo "LEGACY_MQTT_HOST=${host:-<missing>}"
  echo "LEGACY_MQTT_CONNECTED=$connected"
  return 1
}
