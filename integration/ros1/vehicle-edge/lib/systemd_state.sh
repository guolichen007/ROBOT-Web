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
    enabled|disabled|static|masked|indirect|generated|transient|linked|linked-runtime|alias|not-found) printf '%s\n' "$out" ;;
    *) printf '%s\n' "unknown" ;;
  esac
}
