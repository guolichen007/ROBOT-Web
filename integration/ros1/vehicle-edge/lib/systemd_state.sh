# systemd_state.sh — systemd 状态唯一规范化入口（stdout 与 exit code 分离，unknown fail-closed）。
#
# 真实 systemd 语义：
#   is-active  active   → stdout=active   exit 0
#   is-active  inactive → stdout=inactive exit != 0（systemd 用 3）
#   is-enabled enabled  → stdout=enabled  exit 0
#   is-enabled disabled → stdout=disabled exit != 0
#
# 因此绝不能写「$(systemctl is-active X 2>/dev/null || echo inactive)」：
# 当 is-active 输出 inactive 且 exit!=0 时，|| echo inactive 会把结果拼成两行
# 「inactive\ninactive」，破坏精确比较（真实 systemd 下漏检/误判）。
#
# 本 helper 只取 stdout 最后一行，忽略 exit code，并把无法识别的状态 fail-closed 为 unknown。
# 调用方必须把 unknown 视为「无法可靠确定」，按 fail-closed 处理。
#
# 依赖：调用方需已定义 $SYSTEMCTL（默认 systemctl）。
# 用法：source 本文件后调用 unit_active_state <unit> / unit_enabled_state <unit>。

# shellcheck disable=SC2120
unit_active_state() {
  local out
  out="$("${SYSTEMCTL:-systemctl}" is-active "$1" 2>/dev/null || true)"
  out="$(printf '%s\n' "$out" | tail -n1)"
  case "$out" in
    active|inactive|failed|activating|deactivating|reloading) printf '%s\n' "$out" ;;
    *) printf '%s\n' "unknown" ;;
  esac
}

# shellcheck disable=SC2120
unit_enabled_state() {
  local out
  out="$("${SYSTEMCTL:-systemctl}" is-enabled "$1" 2>/dev/null || true)"
  out="$(printf '%s\n' "$out" | tail -n1)"
  case "$out" in
    # enabled-runtime 视同 enabled（开机/运行时会被拉起）；bad/空/其他 → unknown（调用方必须 fail-closed）
    enabled|enabled-runtime) printf '%s\n' "enabled" ;;
    disabled|static|masked|indirect|generated|transient|linked|linked-runtime|alias|not-found) printf '%s\n' "$out" ;;
    *) printf '%s\n' "unknown" ;;
  esac
}

# 便捷判定（fail-closed）：只有明确的 disabled 类状态才算“未启用”，只有 inactive/failed 才算“未运行”；
# unknown（D-Bus 不可达 / bad / 未知输出）一律视为“可能启用/可能运行”。
unit_is_surely_disabled() {
  case "$(unit_enabled_state "$1")" in
    disabled|static|masked|indirect|generated|transient|linked|linked-runtime|alias|not-found) return 0 ;;
    *) return 1 ;;
  esac
}
unit_is_surely_stopped() {
  case "$(unit_active_state "$1")" in
    inactive|failed) return 0 ;;
    *) return 1 ;;
  esac
}
