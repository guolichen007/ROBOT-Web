#!/usr/bin/env bash
# canonical fake systemctl（B0-FINAL-HARDENING）。测试 fixture 统一使用本文件。
#
# 状态文件（$FAKE_STATE 下，旧名映射）：
#   firebot-bridge       → bridge_{active,enabled,load}   默认 inactive/disabled/loaded
#   firebot-vehicle-edge → edge_{active,enabled,load}     默认 inactive/disabled/loaded
#   firebot-control      → ctl_{active,enabled,load}      默认 inactive/disabled/loaded
#   tailscaled           → tailscaled_active              默认 active（migration invariant）
#   systemd-timesyncd    → timesyncd_active               默认 inactive
#   MainPID（vehicle-edge）→ main_pid                     默认 0
#
# 支持：
#   show <unit> --property=LoadState --property=ActiveState --property=UnitFileState --property=MainPID --no-pager
#   show <unit> -p MainPID --value
#   is-active / is-enabled（兼容旧调用路径，真实 exit 语义）
#   enable --now / disable --now（mutation，RC/post-state 注入）
#   daemon-reload
#
# 注入（环境变量）：
#   FAKE_SHOW_FAIL=1            → show 失败（无输出 exit 1，模拟 DBus/systemd 故障 → unknown）
#   FAKE_MUTATION_RC=<n>        → mutation 返回码（默认 0）
#   FAKE_POST_LOAD / FAKE_POST_ACTIVE / FAKE_POST_ENABLED → mutation 后覆盖写入
#
# 日志：$FAKE_LOG（若设置）记录每条调用。

fname() { # <unit> <field>
  case "$1" in
    firebot-bridge) case "$2" in active) echo bridge_active;; enabled) echo bridge_enabled;; load) echo bridge_load;; mainpid) echo bridge_mainpid;; esac;;
    firebot-vehicle-edge) case "$2" in active) echo edge_active;; enabled) echo edge_enabled;; load) echo edge_load;; mainpid) echo main_pid;; esac;;
    firebot-control) case "$2" in active) echo ctl_active;; enabled) echo ctl_enabled;; load) echo ctl_load;; mainpid) echo ctl_mainpid;; esac;;
    tailscaled) case "$2" in active) echo tailscaled_active;; enabled) echo tailscaled_enabled;; load) echo tailscaled_load;; mainpid) echo tailscaled_mainpid;; esac;;
    systemd-timesyncd) case "$2" in active) echo timesyncd_active;; enabled) echo timesyncd_enabled;; load) echo timesyncd_load;; mainpid) echo timesyncd_mainpid;; esac;;
    *) echo "$1.$2";;
  esac
}

st() { # <unit> <field> <default>
  local f
  f="$(fname "$1" "$2")"
  cat "$FAKE_STATE/$f" 2>/dev/null || printf '%s\n' "$3"
}
act_default() { [ "$1" = "tailscaled" ] && echo active || echo inactive; }

emit_active() { printf '%s\n' "$1"; [ "$1" = active ]; }
emit_enabled() { printf '%s\n' "$1"; case "$1" in enabled|enabled-runtime|linked|linked-runtime|alias) exit 0;; *) exit 1;; esac; }

args="$*"
[ -n "${FAKE_LOG:-}" ] && echo "$args" >> "$FAKE_LOG"

case "$args" in
  "is-active "*)
    u="${args#is-active }"; emit_active "$(st "$u" active "$(act_default "$u")")" ;;
  "is-enabled "*)
    u="${args#is-enabled }"; emit_enabled "$(st "$u" enabled disabled)" ;;
  "show "*)
    rest="${args#show }"
    u="${rest%% *}"
    if printf '%s' "$rest" | grep -q -- '--property=LoadState'; then
      if [ "${FAKE_SHOW_FAIL:-0}" = "1" ]; then exit 1; fi
      load="$(st "$u" load loaded)"
      active="$(st "$u" active "$(act_default "$u")")"
      enabled="$(st "$u" enabled disabled)"
      pid="$(st "$u" mainpid 0)"
      printf 'LoadState=%s\nActiveState=%s\nUnitFileState=%s\nMainPID=%s\n' "$load" "$active" "$enabled" "$pid"
      exit 0
    fi
    if printf '%s' "$rest" | grep -q -- '-p MainPID'; then
      st "$u" mainpid 0; exit 0
    fi
    echo "unexpected show: $args" >&2; exit 1 ;;
  "enable --now "*|"disable --now "*)
    op="${args%% *}"
    u="${args#*--now }"
    rc="${FAKE_MUTATION_RC:-0}"
    if [ "$op" = "enable" ]; then
      pa="${FAKE_POST_ACTIVE:-active}"; pe="${FAKE_POST_ENABLED:-enabled}"
    else
      pa="${FAKE_POST_ACTIVE:-inactive}"; pe="${FAKE_POST_ENABLED:-disabled}"
    fi
    [ -n "${FAKE_POST_LOAD:-}" ] && printf '%s\n' "$FAKE_POST_LOAD" > "$FAKE_STATE/$(fname "$u" load)"
    printf '%s\n' "$pa" > "$FAKE_STATE/$(fname "$u" active)"
    printf '%s\n' "$pe" > "$FAKE_STATE/$(fname "$u" enabled)"
    if [ "$u" = "firebot-vehicle-edge" ] && [ "$op" = "enable" ] && [ -n "${FAKE_STATUS_JSON:-}" ]; then
      printf '{"mqtt_connected": %s, "boot_id": "%s", "pid": %s, "release_sha": "%s"}' \
        "${FAKE_STATUS_CONNECTED:-true}" "${FAKE_BOOT_ID:-boot-fixture}" "${FAKE_MAIN_PID:-4242}" "${FAKE_RELEASE_SHA:-}" > "$FAKE_STATUS_JSON"
      printf '%s\n' "${FAKE_MAIN_PID:-4242}" > "$FAKE_STATE/$(fname "$u" mainpid)"
    fi
    if [ "$u" = "firebot-vehicle-edge" ] && [ "$op" = "disable" ]; then
      printf '0\n' > "$FAKE_STATE/$(fname "$u" mainpid)"
    fi
    exit "$rc" ;;
  "daemon-reload") exit 0 ;;
  *) echo "unexpected systemctl: $args" >&2; exit 1 ;;
esac
