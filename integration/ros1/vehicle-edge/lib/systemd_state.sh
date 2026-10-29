# systemd_state.sh — 唯一 systemd 状态真源（systemctl show 权威读取）。
#
# 生产 gate 不再依赖 is-active / is-enabled（stdout+exit 叠加已证明易漏检）。
# 统一使用 `systemctl show <unit> --property=LoadState --property=ActiveState
#   --property=UnitFileState --property=MainPID --no-pager`。
#
# fail-closed：systemctl show 非 0 / 字段为空 / 字段无法识别 / DBus 失败 → unknown。
# 绝不猜 inactive / disabled。
#
# ActiveState 枚举：active inactive failed activating deactivating reloading（其他 unknown）
# UnitFileState ENABLED_LIKE：enabled enabled-runtime linked linked-runtime alias
# UnitFileState EXPLICIT_DISABLED：disabled masked masked-runtime
# UnitFileState 不自动安全（FAIL_CLOSED 人工确认）：static indirect generated transient unknown
#
# 提供：
#   查询：unit_show_all unit_load_state unit_active_state unit_enabled_state unit_main_pid
#   domain predicates：unit_is_enabled_like unit_is_explicitly_disabled
#     unit_is_active_strict unit_is_down_strict control_is_safe tailscale_is_safe
#   mutation wrapper：unit_disable_now_checked unit_enable_now_checked
#     （暴露 SYSTEMD_MUTATION_RC + POST_LOAD_STATE POST_ACTIVE_STATE POST_ENABLED_STATE）
#
# 依赖：调用方需已定义 $SYSTEMCTL（默认 systemctl）。

# ---- 权威查询 ----
unit_show_all() {
  local unit="$1"
  local out rc=0
  out="$("${SYSTEMCTL:-systemctl}" show "$unit" \
    --property=LoadState --property=ActiveState --property=UnitFileState --property=MainPID \
    --no-pager 2>/dev/null)" || rc=$?
  UNIT_SHOW_RC=$rc

  # P0-1：systemctl show 非零返回必须真正 fail-closed。
  # 即使 stdout 恰好残留 loaded/inactive/disabled，也绝不能继续通过 gate。
  if [ "$rc" -ne 0 ]; then
    UNIT_SHOW_LOAD="unknown"
    UNIT_SHOW_ACTIVE="unknown"
    UNIT_SHOW_ENABLED="unknown"
    UNIT_SHOW_MAINPID="unknown"
    return 0
  fi

  UNIT_SHOW_LOAD="$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p' | tail -n1)"
  UNIT_SHOW_ACTIVE="$(printf '%s\n' "$out" | sed -n 's/^ActiveState=//p' | tail -n1)"
  UNIT_SHOW_ENABLED="$(printf '%s\n' "$out" | sed -n 's/^UnitFileState=//p' | tail -n1)"
  UNIT_SHOW_MAINPID="$(printf '%s\n' "$out" | sed -n 's/^MainPID=//p' | tail -n1)"
  [ -n "$UNIT_SHOW_LOAD" ] || UNIT_SHOW_LOAD="unknown"
  [ -n "$UNIT_SHOW_ACTIVE" ] || UNIT_SHOW_ACTIVE="unknown"
  [ -n "$UNIT_SHOW_ENABLED" ] || UNIT_SHOW_ENABLED="unknown"
  [ -n "$UNIT_SHOW_MAINPID" ] || UNIT_SHOW_MAINPID="unknown"
}

unit_load_state() { unit_show_all "$1"; printf '%s\n' "$UNIT_SHOW_LOAD"; }

unit_active_state() {
  unit_show_all "$1"
  case "$UNIT_SHOW_ACTIVE" in
    active|inactive|failed|activating|deactivating|reloading) printf '%s\n' "$UNIT_SHOW_ACTIVE" ;;
    *) printf 'unknown\n' ;;
  esac
}

unit_enabled_state() {
  unit_show_all "$1"

  # systemctl show 查询本身失败时，unit_show_all 已把 UNIT_SHOW_LOAD 归一为 unknown。
  if [ "$UNIT_SHOW_LOAD" = "unknown" ]; then
    printf 'unknown\n'
    return 0
  fi

  # 真实 absent-unit 语义：LoadState=not-found 时 UnitFileState 常为空。
  # 这里统一暴露为 not-found，与 control_is_safe（CONTROL_UNIT_PRESENT=NO）一致，
  # 避免把「unit 不存在」误判成「systemctl 查询不确定」。
  if [ "$UNIT_SHOW_LOAD" = "not-found" ]; then
    printf 'not-found\n'
    return 0
  fi

  case "$UNIT_SHOW_ENABLED" in
    enabled|enabled-runtime|disabled|masked|masked-runtime|static|indirect|generated|transient|linked|linked-runtime|alias) printf '%s\n' "$UNIT_SHOW_ENABLED" ;;
    *) printf 'unknown\n' ;;
  esac
}

unit_main_pid() { unit_show_all "$1"; printf '%s\n' "$UNIT_SHOW_MAINPID"; }

# ---- domain predicates ----
unit_is_enabled_like() {
  unit_show_all "$1"
  case "$UNIT_SHOW_ENABLED" in
    enabled|enabled-runtime|linked|linked-runtime|alias) return 0 ;;
    *) return 1 ;;
  esac
}

unit_is_explicitly_disabled() {
  unit_show_all "$1"
  case "$UNIT_SHOW_ENABLED" in
    disabled|masked|masked-runtime) return 0 ;;
    *) return 1 ;;
  esac
}

unit_is_active_strict() {
  unit_show_all "$1"
  [ "$UNIT_SHOW_ACTIVE" = "active" ]
}

# down-strict：ActiveState=inactive 且（LoadState=not-found 或 explicitly-disabled）。
# allow_absent=yes（默认）时 LoadState=not-found 也视为 down（unit 不存在）。
# allow_absent=no 时 unit 必须存在且 down。
unit_is_down_strict() {
  local unit="$1" allow_absent="${2:-yes}"
  unit_show_all "$unit"
  [ "$UNIT_SHOW_ACTIVE" = "inactive" ] || return 1
  if [ "$UNIT_SHOW_LOAD" = "not-found" ]; then
    [ "$allow_absent" = "yes" ] && return 0 || return 1
  fi
  case "$UNIT_SHOW_ENABLED" in
    disabled|masked|masked-runtime) return 0 ;;
    *) return 1 ;;
  esac
}

# control_is_safe：只接受
#   情况 A：LoadState=not-found（unit 不存在）→ CONTROL_SAFE=YES CONTROL_UNIT_PRESENT=NO
#   情况 B：unit 存在且 ActiveState=inactive 且 UnitFileState∈{disabled,masked,masked-runtime}
#           → CONTROL_SAFE=YES CONTROL_UNIT_PRESENT=YES
# 其余（failed/unknown/static/enabled-runtime/active/...）→ CONTROL_SAFE=NO。
control_is_safe() {
  unit_show_all "firebot-control"
  if [ "$UNIT_SHOW_LOAD" = "not-found" ]; then
    echo "CONTROL_SAFE=YES"
    echo "CONTROL_UNIT_PRESENT=NO"
    return 0
  fi
  if [ "$UNIT_SHOW_LOAD" = "loaded" ] && [ "$UNIT_SHOW_ACTIVE" = "inactive" ]; then
    case "$UNIT_SHOW_ENABLED" in
      disabled|masked|masked-runtime)
        echo "CONTROL_SAFE=YES"
        echo "CONTROL_UNIT_PRESENT=YES"
        return 0
        ;;
    esac
  fi
  echo "CONTROL_SAFE=NO"
  case "$UNIT_SHOW_LOAD" in
    loaded) echo "CONTROL_UNIT_PRESENT=YES" ;;
    not-found) echo "CONTROL_UNIT_PRESENT=NO" ;;
    *) echo "CONTROL_UNIT_PRESENT=UNKNOWN" ;;
  esac
  return 1
}

# tailscale_is_safe：Tailscale 是正式 migration invariant，cutover 前必须 active。
tailscale_is_safe() {
  unit_show_all "tailscaled"
  if [ "$UNIT_SHOW_ACTIVE" = "active" ]; then
    echo "TAILSCALE_STATE=active"
    return 0
  fi
  echo "TAILSCALE_STATE=${UNIT_SHOW_ACTIVE}"
  return 1
}

# ---- mutation wrapper（四象限）----
# 执行后暴露 SYSTEMD_MUTATION_RC + POST_LOAD_STATE POST_ACTIVE_STATE POST_ENABLED_STATE。
# 无论 command RC 如何，都重新读取真实 post-state。
unit_disable_now_checked() {
  local unit="$1"
  SYSTEMD_MUTATION_RC=0
  "${SYSTEMCTL:-systemctl}" disable --now "$unit" >/dev/null 2>&1 || SYSTEMD_MUTATION_RC=$?
  unit_show_all "$unit"
  POST_LOAD_STATE="$UNIT_SHOW_LOAD"
  POST_ACTIVE_STATE="$UNIT_SHOW_ACTIVE"
  POST_ENABLED_STATE="$UNIT_SHOW_ENABLED"
}

unit_enable_now_checked() {
  local unit="$1"
  SYSTEMD_MUTATION_RC=0
  "${SYSTEMCTL:-systemctl}" enable --now "$unit" >/dev/null 2>&1 || SYSTEMD_MUTATION_RC=$?
  unit_show_all "$unit"
  POST_LOAD_STATE="$UNIT_SHOW_LOAD"
  POST_ACTIVE_STATE="$UNIT_SHOW_ACTIVE"
  POST_ENABLED_STATE="$UNIT_SHOW_ENABLED"
}
