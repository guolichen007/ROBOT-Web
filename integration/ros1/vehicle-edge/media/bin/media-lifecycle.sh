#!/usr/bin/env bash
# media-lifecycle.sh — firebotctl vehicle media <sub> 的唯一实现（新车与已有车辆共用一套，无车辆分支）。
#
# 纳入 Vehicle Edge 的 release/config/secrets/runtime 体系（不另建部署架构）：
#   <EDGE_HOME>/media/releases/<SHA>/   immutable 媒体 release（firebot_media + lib/libsrt + MANIFEST）
#   <EDGE_HOME>/media/current|previous  原子切换（临时 symlink + mv -T）
#   <EDGE_HOME>/config/media-hardware.env  Hardware Profile（root 0644）
#   <EDGE_HOME>/config/media.env           车辆/endpoint（root 0644，非 secret）
#   <EDGE_HOME>/media/secrets/             srt_publish_token / srt_publish_passphrase（<媒体用户> 0600，目录 0700）
#   <EDGE_HOME>/media/state/               运行状态 JSON（<媒体用户> 0750）
#   /etc/systemd/system/firebot-media@.service（渲染自模板）
#
# 铁律：
#   - 安装 ≠ 启动：install/credentials 后 firebot-media@* 保持 inactive + disabled；只有 start 启动。
#   - 不触碰：firebot-vehicle-agent-v1-readonly / firebot-vehicle-edge / firebot-bridge / firebot-control /
#     ROS workspace / MQTT 凭证 / enrollment（001 增量补齐不得停止现有 readonly agent）。
#   - 凭证：只接受受控文件或 stdin（交互输入不回显）；绝不经 argv；主密钥永不下发到车端。
#
# 子命令：
#   install --sha <SHA> [--libsrt-source <tar.gz>] [--libsrt-prebuilt <dir>] [--hardware-profile <ID>]
#   credentials import --vehicle-id <ID> --srt-host <H> --srt-port <P> --bundle-file <FILE>|--bundle-stdin
#   credentials import --from-enrollment   （新车：enroll 下发的 secrets/media-bundle.json，含 endpoint）
#   preflight                 配置/凭证/libsrt/编码器/源可达（不推流、不启动）
#   start [<camera>|all]      enable --now firebot-media@<camera>
#   stop  [<camera>|all]      disable --now
#   status                    systemd + media/state/*.json（无 secret）
#   rollback                  current ← previous（先停服务；回滚后保持 stopped，由运维 start）
#   doctor                    只读诊断（源/设备占用/编码器/libsrt/网络 UDP 可达）
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_SRC="$(cd "$SELF_DIR/.." && pwd)"            # integration/ros1/vehicle-edge/media
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
SYSTEMD_DIR="${FIREBOT_TEST_SYSTEMD_DIR:-/etc/systemd/system}"
PYTHON="${FIREBOT_MEDIA_PYTHON:-/usr/bin/python3}"
MEDIA_USER_DEFAULT="firebot-media"
UNIT_TEMPLATE="$MEDIA_SRC/systemd/firebot-media@.service.template"
MEDIA_HOME="$EDGE_HOME/media"
CFG="$EDGE_HOME/config"
SECRETS="$MEDIA_HOME/secrets"
STATE="$MEDIA_HOME/state"
PROTECTED_UNITS="firebot-vehicle-agent-v1-readonly firebot-vehicle-edge firebot-bridge firebot-control"

fail() { echo "MEDIA_$1=FAIL（$2）" >&2; exit "${3:-1}"; }
need_root() {
  [ "${FIREBOT_TEST_SKIP_ROOT:-0}" = "1" ] && return 0
  [ "$(id -u)" -eq 0 ] || fail "${1:-OP}" "需要 root（sudo）"
}
skip_chown() { [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" = "1" ]; }

media_user() {
  local u=""
  [ -f "$CFG/install.env" ] && u="$(grep -E '^FIREBOT_MEDIA_USER=' "$CFG/install.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
  echo "${u:-$MEDIA_USER_DEFAULT}"
}

cameras() { # media.env 覆盖 media-hardware.env（与发布器 config.py 同语义）
  local c="" f
  for f in "$CFG/media-hardware.env" "$CFG/media.env"; do
    [ -f "$f" ] || continue
    local v; v="$(grep -E '^FIREBOT_MEDIA_CAMERAS=' "$f" | tail -1 | cut -d= -f2- || true)"
    [ -n "$v" ] && c="$v"
  done
  echo "$c" | tr ',' ' '
}

# 受保护单元快照（用于证明媒体操作不影响 G1/edge/control）
protected_snapshot() {
  local out="" u
  for u in $PROTECTED_UNITS; do
    out="$out $u=$("$SYSTEMCTL" is-active "$u" 2>/dev/null || true)/$("$SYSTEMCTL" is-enabled "$u" 2>/dev/null || true)"
  done
  echo "$out"
}

atomic_link() { # atomic_link <target-rel> <linkpath>
  local tmp="$2.tmp.$$"
  ln -sfn "$1" "$tmp"
  mv -Tf "$tmp" "$2"
}

ensure_user() {
  local u="$1"
  if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" = "1" ]; then return 0; fi
  if ! id "$u" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$u"
  fi
  for g in video render; do getent group "$g" >/dev/null 2>&1 && usermod -aG "$g" "$u" || true; done
}

layout() {
  local u; u="$(media_user)"
  mkdir -p "$MEDIA_HOME/releases" "$SECRETS" "$STATE"
  chmod 0755 "$MEDIA_HOME" "$MEDIA_HOME/releases"
  chmod 0700 "$SECRETS"
  chmod 0750 "$STATE"
  if ! skip_chown; then
    chown root:root "$MEDIA_HOME" "$MEDIA_HOME/releases"
    chown "$u:$u" "$SECRETS" "$STATE"
  fi
}

# ------------------------------------------------------------------ install
cmd_install() {
  local sha="" libsrt_src="" libsrt_pre="" hwp="firebot_ros1_standard_v1"
  while [ $# -gt 0 ]; do
    case "$1" in
      --sha) sha="${2:-}"; shift 2 ;;
      --libsrt-source) libsrt_src="${2:-}"; shift 2 ;;
      --libsrt-prebuilt) libsrt_pre="${2:-}"; shift 2 ;;
      --hardware-profile) hwp="${2:-}"; shift 2 ;;
      *) fail INSTALL "未知参数 $1" 2 ;;
    esac
  done
  need_root INSTALL
  echo "$sha" | grep -qE '^[0-9a-f]{40}$' || fail INSTALL "--sha 必须 40 位 hex" 2
  echo "$hwp" | grep -qE '^[a-z0-9_]{1,64}$' || fail INSTALL "--hardware-profile 非法" 2
  local repo; repo="$(cd "$MEDIA_SRC/../../../.." && pwd)"
  if [ "${FIREBOT_TEST_SKIP_GIT:-0}" != "1" ]; then
    [ -z "$(git -C "$repo" status --porcelain 2>/dev/null)" ] || { echo "MEDIA_INSTALL=REJECTED"; echo "REPO_NOT_CLEAN"; exit 1; }
    [ "$(git -C "$repo" rev-parse HEAD)" = "$sha" ] || { echo "MEDIA_INSTALL=REJECTED"; echo "HEAD_SHA_MISMATCH"; exit 1; }
  fi
  [ -d "$EDGE_HOME/config" ] || { echo "MEDIA_INSTALL=REJECTED"; echo "EDGE_HOME_NOT_INITIALIZED（先 firebotctl vehicle edge-init）"; exit 1; }
  local profile="$MEDIA_SRC/profiles/$hwp.media.env"
  [ -f "$profile" ] || fail INSTALL "Hardware Profile 不存在: $hwp"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || fail INSTALL "需要 python3 ≥ 3.8"

  local before; before="$(protected_snapshot)"
  local u; u="$(media_user)"
  ensure_user "$u"
  if [ ! -f "$CFG/install.env" ] || ! grep -qE '^FIREBOT_MEDIA_USER=' "$CFG/install.env"; then
    echo "FIREBOT_MEDIA_USER=$u" >> "$CFG/install.env"
  fi
  layout

  local rel="$MEDIA_HOME/releases/$sha"
  if [ -e "$rel" ]; then
    if [ -f "$rel/MANIFEST" ] && grep -q "^source_sha=$sha$" "$rel/MANIFEST" && [ -f "$rel/lib/libsrt.so.1.4" ] \
       && (cd "$rel" && sha256sum -c --quiet MANIFEST.sha256 >/dev/null 2>&1); then
      echo "MEDIA_RELEASE=EXISTS_VERIFIED sha=$sha"
    else
      echo "MEDIA_INSTALL=REJECTED"; echo "RELEASE_CORRUPT_OR_MISMATCH（releases/$sha 已存在但校验失败；禁止自动覆盖）"; exit 1
    fi
  else
    local staging; staging="$(mktemp -d "$MEDIA_HOME/releases/.staging.XXXXXX")"
    # 安装中断（任一步失败）→ staging 删除，current 不变
    trap 'rm -rf "'"$staging"'"' EXIT
    mkdir -p "$staging/lib"
    cp -r --no-preserve=ownership "$MEDIA_SRC/firebot_media" "$staging/firebot_media"
    find "$staging" -name '__pycache__' -prune -exec rm -rf {} +
    if [ -n "$libsrt_pre" ]; then
      [ -f "$libsrt_pre/libsrt.so.1.4.4" ] && [ -f "$libsrt_pre/libsrt.so.1.4.4.sha256" ] || fail INSTALL "--libsrt-prebuilt 缺少 libsrt.so.1.4.4(.sha256)"
      [ "$(sha256sum "$libsrt_pre/libsrt.so.1.4.4" | awk '{print $1}')" = "$(cat "$libsrt_pre/libsrt.so.1.4.4.sha256")" ] || fail INSTALL "预构建 libsrt SHA256 不符"
      cp -r --no-preserve=ownership "$libsrt_pre/." "$staging/lib/"
    else
      bash "$MEDIA_SRC/bin/build-libsrt.sh" "$staging/lib" ${libsrt_src:+--source "$libsrt_src"} || fail INSTALL "libsrt 构建失败"
    fi
    ln -sfn libsrt.so.1.4.4 "$staging/lib/libsrt.so.1.4"
    # 自检：用目标 python 加载本 release 的 libsrt 并校验版本 ≥ 1.4.1（失败即拒绝落位）
    PYTHONPATH="$staging" FIREBOT_MEDIA_LIBSRT="$staging/lib/libsrt.so.1.4" "$PYTHON" - <<'PY' || fail INSTALL "release 自检失败（libsrt 加载/版本）"
import os
from firebot_media.srt import load_lib, library_version
lib = load_lib(os.environ["FIREBOT_MEDIA_LIBSRT"])
print("MEDIA_LIBSRT_SELFTEST=PASS version=%s" % library_version(lib))
PY
    { echo "product=firebot-media"; echo "source_sha=$sha"; echo "hardware_profile=$hwp"; } > "$staging/MANIFEST"
    (cd "$staging" && find firebot_media lib MANIFEST -type f -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256)
    chmod -R u=rwX,go=rX "$staging"
    skip_chown || chown -R root:root "$staging"
    mv "$staging" "$rel"
    trap - EXIT
    echo "MEDIA_RELEASE=PASS sha=$sha"
  fi

  # Hardware Profile 落位（已存在且内容不同 → 保留现场值，提示 diff；绝不静默覆盖）
  if [ -f "$CFG/media-hardware.env" ] && ! cmp -s "$profile" "$CFG/media-hardware.env"; then
    echo "MEDIA_HARDWARE_PROFILE=KEPT_LOCAL（config/media-hardware.env 与仓库 $hwp 不同；保留现场值）"
  else
    install -m 0644 "$profile" "$CFG/media-hardware.env"
    echo "MEDIA_HARDWARE_PROFILE=INSTALLED profile=$hwp"
  fi

  # systemd unit（新 unit；不改造任何既有 unit）
  local tmp; tmp="$(mktemp)"
  sed -e "s#@EDGE_HOME@#$EDGE_HOME#g" -e "s#@MEDIA_USER@#$u#g" "$UNIT_TEMPLATE" > "$tmp"
  chmod 0644 "$tmp"
  mkdir -p "$SYSTEMD_DIR"
  mv "$tmp" "$SYSTEMD_DIR/firebot-media@.service"
  [ "${FIREBOT_TEST_SKIP_ROOT:-0}" = "1" ] || "$SYSTEMCTL" daemon-reload

  # 切换：先记录 previous，再原子切 current（运行中的发布器不受影响，restart 后生效）
  if [ -L "$MEDIA_HOME/current" ] && [ "$(readlink "$MEDIA_HOME/current")" != "releases/$sha" ]; then
    atomic_link "$(readlink "$MEDIA_HOME/current")" "$MEDIA_HOME/previous"
  fi
  atomic_link "releases/$sha" "$MEDIA_HOME/current"

  local after; after="$(protected_snapshot)"
  [ "$before" = "$after" ] || fail INSTALL "受保护单元状态发生变化（$before → $after）"
  echo "MEDIA_PROTECTED_UNITS_UNCHANGED=PASS"
  local c running=""
  for c in $(cameras); do
    case "$("$SYSTEMCTL" is-active "firebot-media@$c" 2>/dev/null || true)" in
      active|activating) running="$running $c" ;;
    esac
  done
  [ -z "$running" ] && echo "MEDIA_SERVICE_STATE=inactive（安装≠启动）" \
    || echo "MEDIA_SERVICE_STATE=RUNNING_OLD_RELEASE:$running（升级：执行 media start 以重启到新 release）"
  echo "MEDIA_INSTALL=PASS sha=$sha"
}

# ------------------------------------------------------------------ credentials
# bundle 格式（JSON，单车，不含主密钥）：
#   {"kind":"firebot.media-credentials.v1","vehicle_id":"...","srt_publish_token":"<64hex>",
#    "srt_publish_passphrase":"..."}
cmd_credentials() {
  [ "${1:-}" = "import" ] || fail CREDENTIALS "用法: media credentials import ..." 2
  shift
  local vid="" host="" port="" file="" from_stdin=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --vehicle-id) vid="${2:-}"; shift 2 ;;
      --srt-host) host="${2:-}"; shift 2 ;;
      --srt-port) port="${2:-}"; shift 2 ;;
      --bundle-file) file="${2:-}"; shift 2 ;;
      --bundle-stdin) from_stdin=1; shift ;;
      --from-enrollment)
        file="$EDGE_HOME/secrets/media-bundle.json"
        [ -f "$file" ] || fail CREDENTIALS "未找到 enrollment 下发的 secrets/media-bundle.json（先 vehicle enroll）"
        local ep
        ep="$("$PYTHON" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("vehicle_id",""),d.get("srt_host",""),d.get("srt_port",""))' "$file")" \
          || fail CREDENTIALS "media-bundle.json 非法"
        read -r vid host port <<<"$ep"
        shift ;;
      --token|--passphrase|--srt-publish-token) fail CREDENTIALS "禁止经命令行参数传递凭证（使用 --bundle-file 或 --bundle-stdin）" 2 ;;
      *) fail CREDENTIALS "未知参数 $1" 2 ;;
    esac
  done
  need_root CREDENTIALS
  echo "$vid" | grep -qE '^[A-Za-z0-9_-]{1,64}$' || fail CREDENTIALS "--vehicle-id 非法" 2
  echo "$host" | grep -qE '^[A-Za-z0-9.-]{1,253}$' || fail CREDENTIALS "--srt-host 非法" 2
  echo "$port" | grep -qE '^[0-9]{1,5}$' || fail CREDENTIALS "--srt-port 非法" 2
  [ -d "$MEDIA_HOME" ] || fail CREDENTIALS "先 media install"
  if [ -f "$CFG/identity.env" ]; then
    local enrolled; enrolled="$(grep -E '^DEVICE_ID=' "$CFG/identity.env" | cut -d= -f2- || true)"
    [ -z "$enrolled" ] || [ "$enrolled" = "$vid" ] || fail CREDENTIALS "vehicle_id 与 Enrollment 身份（$enrolled）不一致"
  fi
  local u; u="$(media_user)"
  layout
  local tmpdir; tmpdir="$(mktemp -d "$SECRETS/.import.XXXXXX")"
  trap 'rm -rf "'"$tmpdir"'"' EXIT
  local src
  if [ "$from_stdin" = 1 ]; then src=/dev/stdin; else
    [ -n "$file" ] || fail CREDENTIALS "必须 --bundle-file 或 --bundle-stdin" 2
    [ -f "$file" ] || fail CREDENTIALS "bundle 文件不存在"
    local fm; fm="$(stat -c %a "$file")"
    case "$fm" in 600|400) : ;; *) fail CREDENTIALS "bundle 文件权限必须 0600/0400（当前 $fm）" ;; esac
    src="$file"
  fi
  # python 进程内解析与写入：值不经 argv、不回显；临时文件 0600 后原子 rename
  FIREBOT_MEDIA_VID="$vid" FIREBOT_MEDIA_TMP="$tmpdir" "$PYTHON" -c '
import json, os, re, sys
data = json.load(sys.stdin)
vid = os.environ["FIREBOT_MEDIA_VID"]
tmp = os.environ["FIREBOT_MEDIA_TMP"]
if data.get("kind") != "firebot.media-credentials.v1":
    sys.exit("bundle kind 不匹配")
if data.get("vehicle_id") != vid:
    sys.exit("bundle vehicle_id 与 --vehicle-id 不一致（错误身份）")
if "media_publish_token" in data or "master" in json.dumps(list(data.keys())):
    sys.exit("bundle 含主密钥字段：拒绝（主密钥永不下发车端）")
tok = str(data.get("srt_publish_token", "")).strip()
pp = str(data.get("srt_publish_passphrase", "")).strip()
if not re.fullmatch(r"[0-9a-f]{64}", tok):
    sys.exit("srt_publish_token 格式异常")
if not 10 <= len(pp.encode()) <= 79:
    sys.exit("srt_publish_passphrase 长度异常")
for name, val in (("srt_publish_token", tok), ("srt_publish_passphrase", pp)):
    p = os.path.join(tmp, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(val + "\n")
print("BUNDLE_OK")
' < "$src" >/dev/null || fail CREDENTIALS "bundle 校验失败（见上；值不回显）"
  local n
  for n in srt_publish_token srt_publish_passphrase; do
    skip_chown || chown "$u:$u" "$tmpdir/$n"
    chmod 0600 "$tmpdir/$n"
    mv -f "$tmpdir/$n" "$SECRETS/$n"
  done
  rm -rf "$tmpdir"; trap - EXIT
  local tmpenv; tmpenv="$(mktemp "$CFG/.media.env.XXXXXX")"
  {
    echo "# GENERATED by firebotctl vehicle media credentials import（非 secret）"
    echo "FIREBOT_MEDIA_VEHICLE_ID=$vid"
    echo "FIREBOT_MEDIA_SRT_HOST=$host"
    echo "FIREBOT_MEDIA_SRT_PORT=$port"
  } > "$tmpenv"
  if [ -f "$CFG/media.env" ]; then
    grep -vE '^(#|FIREBOT_MEDIA_VEHICLE_ID=|FIREBOT_MEDIA_SRT_HOST=|FIREBOT_MEDIA_SRT_PORT=)' "$CFG/media.env" >> "$tmpenv" || true
  fi
  chmod 0644 "$tmpenv"; mv -f "$tmpenv" "$CFG/media.env"
  echo "MEDIA_CREDENTIALS=IMPORTED vehicle_id=$vid endpoint=$host:$port（值不回显；MQTT 未触碰）"
}

# ------------------------------------------------------------------ run helpers
run_py() { # run_py <camera> [args] — 以媒体 release 运行发布器（preflight/doctor）
  local cam="$1"; shift
  [ -L "$MEDIA_HOME/current" ] || fail PREFLIGHT "未安装（media/current 不存在）"
  local u; u="$(media_user)"
  if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ] && [ "$(id -u)" -eq 0 ]; then
    runuser -u "$u" -- env PYTHONPATH="$MEDIA_HOME/current" FIREBOT_EDGE_HOME="$EDGE_HOME" \
      "$PYTHON" -m firebot_media "$cam" --edge-home "$EDGE_HOME" "$@"
  else
    PYTHONPATH="$MEDIA_HOME/current" FIREBOT_EDGE_HOME="$EDGE_HOME" "$PYTHON" -m firebot_media "$cam" --edge-home "$EDGE_HOME" "$@"
  fi
}

cmd_preflight() {
  local c rc=0 cams out; cams="$(cameras)"
  [ -n "$cams" ] || fail PREFLIGHT "未配置 FIREBOT_MEDIA_CAMERAS"
  for c in $cams; do
    if out="$(run_py "$c" --check 2>&1)"; then :; else rc=1; fi
    printf '%s\n' "$out" | sed "s/^/  [$c] /"
  done
  [ "$rc" -eq 0 ] && echo "MEDIA_PREFLIGHT=PASS cameras=${cams// /,}" || { echo "MEDIA_PREFLIGHT=FAIL" >&2; exit 1; }
}

select_cams() { if [ -z "${1:-}" ] || [ "$1" = "all" ]; then cameras; else echo "$1"; fi; }

cmd_start() {
  need_root START
  local c cams; cams="$(select_cams "${1:-}")"
  [ -n "$cams" ] || fail START "无可启动的 camera"
  local before; before="$(protected_snapshot)"
  cmd_preflight >/dev/null || fail START "preflight 未通过（先 media preflight 查看原因）"
  for c in $cams; do
    "$SYSTEMCTL" enable "firebot-media@$c" >/dev/null 2>&1 || fail START "enable firebot-media@$c"
    "$SYSTEMCTL" restart "firebot-media@$c" || fail START "start firebot-media@$c"
  done
  [ "$before" = "$(protected_snapshot)" ] || fail START "受保护单元状态发生变化"
  echo "MEDIA_START=PASS cameras=${cams// /,}（G1/edge/control 未触碰）"
}

cmd_stop() {
  need_root STOP
  local c cams; cams="$(select_cams "${1:-}")"
  local bad=""
  for c in $cams; do
    "$SYSTEMCTL" disable --now "firebot-media@$c" >/dev/null 2>&1 || true
    case "$("$SYSTEMCTL" is-active "firebot-media@$c" 2>/dev/null | tail -n1 || true)" in
      inactive|failed) : ;;
      *) bad="$bad $c" ;;
    esac
  done
  [ -z "$bad" ] || fail STOP "仍在运行或状态未知:$bad"
  echo "MEDIA_STOP=PASS cameras=${cams// /,}"
}

cmd_status() {
  local c
  echo "MEDIA_RELEASE_CURRENT=$(readlink "$MEDIA_HOME/current" 2>/dev/null || echo NONE)"
  echo "MEDIA_RELEASE_PREVIOUS=$(readlink "$MEDIA_HOME/previous" 2>/dev/null || echo NONE)"
  for c in $(cameras); do
    local st="$STATE/$c.json" s="UNKNOWN" sid=""
    if [ -f "$st" ]; then
      s="$("$PYTHON" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("state",""))' "$st" 2>/dev/null || echo UNKNOWN)"
      sid="$("$PYTHON" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("stream_id",""))' "$st" 2>/dev/null || true)"
    fi
    echo "MEDIA_CAMERA camera=$c unit=$("$SYSTEMCTL" is-active "firebot-media@$c" 2>/dev/null || true)/$("$SYSTEMCTL" is-enabled "firebot-media@$c" 2>/dev/null || true) state=$s stream_id=$sid"
  done
}

cmd_rollback() {
  need_root ROLLBACK
  [ -L "$MEDIA_HOME/previous" ] || fail ROLLBACK "无 previous release"
  local prev cur; prev="$(readlink "$MEDIA_HOME/previous")"; cur="$(readlink "$MEDIA_HOME/current")"
  [ -d "$MEDIA_HOME/$prev" ] || fail ROLLBACK "previous 指向不存在的 release"
  (cd "$MEDIA_HOME/$prev" && sha256sum -c --quiet MANIFEST.sha256 >/dev/null 2>&1) || fail ROLLBACK "previous release 校验失败"
  cmd_stop all >/dev/null
  atomic_link "$prev" "$MEDIA_HOME/current"
  atomic_link "$cur" "$MEDIA_HOME/previous"
  echo "MEDIA_ROLLBACK=PASS current=$prev previous=$cur（服务保持停止；确认后 media start）"
}

cmd_doctor() {
  echo "MEDIA_DOCTOR edge_home=$EDGE_HOME user=$(media_user)"
  cmd_status
  local c
  for c in $(cameras); do
    run_py "$c" --check 2>&1 | sed "s/^/  [$c] /" || true
  done
  # 设备占用（仅提示，不打开设备）：谁持有 /dev/video*
  if command -v fuser >/dev/null 2>&1; then
    for d in /dev/video*; do
      [ -e "$d" ] || continue
      echo "  DEVICE $d holders=$(fuser "$d" 2>/dev/null | tr -s ' ' || true)"
    done
  fi
  ls -l /dev/v4l/by-id 2>/dev/null | sed 's/^/  BY_ID /' || true
  echo "MEDIA_DOCTOR=DONE（只读；未启动/停止任何服务）"
}

sub="${1:-}"; shift || true
case "$sub" in
  install) cmd_install "$@" ;;
  credentials) cmd_credentials "$@" ;;
  preflight) cmd_preflight ;;
  start) cmd_start "${1:-all}" ;;
  stop) cmd_stop "${1:-all}" ;;
  status) cmd_status ;;
  rollback) cmd_rollback ;;
  doctor) cmd_doctor ;;
  *) echo "用法: firebotctl vehicle media <install|credentials import|preflight|start|stop|status|rollback|doctor>" >&2; exit 2 ;;
esac
