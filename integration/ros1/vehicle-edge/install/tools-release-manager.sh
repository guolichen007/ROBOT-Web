#!/usr/bin/env bash
# tools-release-manager.sh — Vehicle Edge 管理平面（tools）release 组装与原子切换。
#
# 管理平面与数据平面是两个独立 release domain：
#   数据平面  /home/tl/firebot-edge/releases/<SHA>     （firebot-vehicle-edge 运行时）
#   管理平面  /home/tl/firebot-edge/tools/releases/<SHA> （firebotctl + lifecycle/install/systemd/config）
#
# 职责仅限：build / verify / switch / rollback / status。
# 绝不：enroll / 生成 credential / 启动 edge / 停止 legacy / 启动 control / 操作 ROS。
#
# release 结构（规格九）：
#   tools/releases/<SHA>/
#     VERSION  manifest.json  checksums.sha256  firebotctl
#     vehicle-edge/{lifecycle,install,systemd,config,lib,bin,bridge,monitor}
#
# 不可变：同名 SHA 已存在 → 完整性完全一致 → EXISTS_VERIFIED；不一致 → RELEASE_CORRUPT_OR_MISMATCH。
# 绝不 rm -rf 后重新生成掩盖 artifact 损坏。
#
# 用法：
#   tools-release-manager.sh build    <SOURCE_DIR> <SHA> [EDGE_HOME]
#   tools-release-manager.sh verify   <SHA> [EDGE_HOME]
#   tools-release-manager.sh switch   <SHA> [EDGE_HOME]
#   tools-release-manager.sh rollback [EDGE_HOME]
#   tools-release-manager.sh status   [EDGE_HOME]
set -euo pipefail

EDGE_HOME_DEFAULT="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SOURCE_DIR=""
SHA=""

manifest_json() {
  printf '{\n  "product": "firebot-edge-tools",\n  "schema_version": 1,\n  "source_sha": "%s",\n  "scope": "vehicle-edge-management"\n}\n' "$SHA"
}

require_sha() {
  echo "$1" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: SHA 必须 40 位 hex" >&2; exit 1; }
}

# 打包的 vehicle-edge 子目录（管理平面运行时 + 组装数据平面 release 所需全部源码）。
# 排除 tests/docs/.git/__pycache__：交付物不含测试代码、迁移文档、VCS、编译缓存。
BUNDLE_DIRS="lifecycle install systemd config lib bin bridge monitor"

# 生成 checksums.sha256（覆盖 release 内全部正式文件，自身除外）。
write_checksums() {
  local dir="$1"
  (cd "$dir" && find . -type f ! -name 'checksums.sha256' -print0 \
    | sort -z | xargs -0 sha256sum > checksums.sha256)
}

# 校验 release 完整性（manifest + checksums + 关键文件）。
verify_release() {
  local release="$1" expected="$2"
  grep -q "\"source_sha\": \"$expected\"" "$release/manifest.json" 2>/dev/null || return 1
  (cd "$release" && sha256sum -c checksums.sha256 --quiet >/dev/null 2>&1) || return 1
  [ -f "$release/firebotctl" ] || return 1
  [ -f "$release/vehicle-edge/lifecycle/edge-install.sh" ] || return 1
  [ -f "$release/vehicle-edge/lifecycle/rollback.sh" ] || return 1
  [ -f "$release/vehicle-edge/install/release-manager.sh" ] || return 1
  return 0
}

cmd_build() {
  SOURCE_DIR="${1:?用法: tools-release-manager.sh build <SOURCE_DIR> <SHA> [EDGE_HOME]}"
  SHA="${2:?}"
  EDGE_HOME="${3:-$EDGE_HOME_DEFAULT}"
  require_sha "$SHA"

  # 交付源必须含 firebotctl 与 vehicle-edge 源码。
  [ -f "$SOURCE_DIR/firebotctl" ] || { echo "ERROR: 交付源缺少 firebotctl（$SOURCE_DIR）" >&2; exit 1; }
  [ -d "$SOURCE_DIR/vehicle-edge" ] || { echo "ERROR: 交付源缺少 vehicle-edge（$SOURCE_DIR）" >&2; exit 1; }

  local release="$EDGE_HOME/tools/releases/$SHA"
  local staging
  staging="$(mktemp -d "$EDGE_HOME/tools/releases/.staging.XXXXXX")"

  # 0) 车辆特定代码禁止（NO_VEHICLE_SPECIFIC_PRODUCTION_CODE）。
  if find "$SOURCE_DIR" -maxdepth 3 -type d \( -name 'R00[0-9]' -o -name 'R00[0-9]-*' \) | grep -q .; then
    echo "ERROR: 交付源含车辆特定目录（R00x），禁止组装（车辆只是数据）" >&2
    rm -rf "$staging"
    exit 1
  fi

  # 1) 组装：firebotctl + vehicle-edge 子集（管理平面运行时）。
  mkdir -p "$staging/vehicle-edge"
  cp -a "$SOURCE_DIR/firebotctl" "$staging/firebotctl"
  cp -a "$SOURCE_DIR/vehicle-edge/VERSION" "$staging/VERSION" 2>/dev/null || true
  for d in $BUNDLE_DIRS; do
    [ -e "$SOURCE_DIR/vehicle-edge/$d" ] && cp -a "$SOURCE_DIR/vehicle-edge/$d" "$staging/vehicle-edge/"
  done
  printf '%s\n' "$(manifest_json)" > "$staging/manifest.json"
  # 清理 staging 内任何车辆特定目录（合同：NO_VEHICLE_SPECIFIC_PRODUCTION_CODE）。
  if find "$staging" -type d \( -name 'R00[0-9]' -o -name 'R00[0-9]-*' \) | grep -q .; then
    echo "ERROR: release 含车辆特定目录（R00x），禁止组装" >&2
    rm -rf "$staging"
    exit 1
  fi

  # 2) 完整性：manifest source_sha + 关键文件齐备。
  grep -q "\"source_sha\": \"$SHA\"" "$staging/manifest.json" || {
    echo "ERROR: manifest source_sha 与 SHA 不一致" >&2
    rm -rf "$staging"
    exit 1
  }
  for required in firebotctl manifest.json vehicle-edge/lifecycle/edge-install.sh \
                 vehicle-edge/lifecycle/rollback.sh vehicle-edge/install/release-manager.sh; do
    [ -e "$staging/$required" ] || {
      echo "ERROR: release 缺少 $required" >&2
      rm -rf "$staging"
      exit 1
    }
  done

  # 3) 生成 checksums（覆盖全部正式文件）。
  write_checksums "$staging"

  # 4) 落位 tools/releases/<SHA>（原子 rename；同名 SHA 已存在 → EXISTS_VERIFIED / RELEASE_CORRUPT_OR_MISMATCH）。
  if [ -e "$release" ]; then
    if verify_release "$release" "$SHA"; then
      echo "TOOLS_RELEASE_BUILD=EXISTS_VERIFIED release=$release（同 SHA 安全重试；不覆盖）"
      rm -rf "$staging"
      return 0
    fi
    echo "TOOLS_RELEASE_BUILD=REJECTED"
    echo "RELEASE_CORRUPT_OR_MISMATCH"
    echo "原因：tools/releases/$SHA 已存在但完整性校验不通过；禁止自动删除，由人工处置。" >&2
    rm -rf "$staging"
    exit 1
  fi
  chmod -R u=rwX,go=rX "$staging"
  chmod 0755 "$staging/firebotctl"
  mv "$staging" "$release"
  echo "TOOLS_RELEASE_BUILD=PASS release=$release"
}

cmd_verify() {
  SHA="${1:?用法: tools-release-manager.sh verify <SHA> [EDGE_HOME]}"
  EDGE_HOME="${2:-$EDGE_HOME_DEFAULT}"
  require_sha "$SHA"
  local release="$EDGE_HOME/tools/releases/$SHA"
  [ -d "$release" ] || { echo "TOOLS_RELEASE_VERIFY=FAIL（releases/$SHA 不存在）" >&2; exit 1; }
  if verify_release "$release" "$SHA"; then
    echo "TOOLS_RELEASE_VERIFY=PASS sha=$SHA"
  else
    echo "TOOLS_RELEASE_VERIFY=FAIL（manifest/checksum/关键文件校验失败）" >&2
    exit 1
  fi
}

cmd_switch() {
  SHA="${1:?用法: tools-release-manager.sh switch <SHA> [EDGE_HOME]}"
  EDGE_HOME="${2:-$EDGE_HOME_DEFAULT}"
  require_sha "$SHA"
  local release="$EDGE_HOME/tools/releases/$SHA"
  [ -d "$release" ] || { echo "TOOLS_RELEASE_SWITCH=FAIL（tools/releases/$SHA 不存在）" >&2; exit 1; }
  verify_release "$release" "$SHA" || {
    echo "TOOLS_RELEASE_SWITCH=FAIL（release 完整性校验失败）" >&2
    exit 1
  }

  # 同 SHA 幂等重试（B0-FIX2）：current 已指向本 SHA → ALREADY_CURRENT，
  # 绝不把 tools/previous 改成本 SHA（必须保留真正回滚指针）。
  local cur=""
  if [ -L "$EDGE_HOME/tools/current" ]; then
    cur="$(readlink "$EDGE_HOME/tools/current")"
  fi
  if [ -n "$cur" ] && [ "$(basename "$cur")" = "$SHA" ]; then
    local prev_name="none"
    if [ -L "$EDGE_HOME/tools/previous" ]; then
      prev_name="$(basename "$(readlink "$EDGE_HOME/tools/previous")")"
    fi
    echo "TOOLS_RELEASE_SWITCH=ALREADY_CURRENT"
    echo "TOOLS_CURRENT_SHA=$SHA"
    echo "TOOLS_PREVIOUS_SHA=$prev_name"
    return 0
  fi

  # 原子 current/previous 切换：临时 symlink + mv（POSIX rename 语义）。
  local old=""
  if [ -L "$EDGE_HOME/tools/current" ]; then
    old="$(readlink "$EDGE_HOME/tools/current")"
  fi
  local tmp_current="$EDGE_HOME/tools/.current.tmp.$$"
  ln -s "releases/$SHA" "$tmp_current"
  if [ -n "$old" ]; then
    rm -f "$EDGE_HOME/tools/previous"
    ln -s "$old" "$EDGE_HOME/tools/previous"
  fi
  mv -Tf "$tmp_current" "$EDGE_HOME/tools/current"
  [ "$(readlink "$EDGE_HOME/tools/current")" = "releases/$SHA" ] || {
    echo "TOOLS_RELEASE_SWITCH=FAIL（current 切换后校验失败）" >&2
    exit 1
  }
  local prev_name="none"
  [ -n "$old" ] && prev_name="$(basename "$old")"
  echo "TOOLS_RELEASE_SWITCH=PASS"
  echo "TOOLS_CURRENT_SHA=$SHA"
  echo "TOOLS_PREVIOUS_SHA=$prev_name"
}

cmd_rollback() {
  EDGE_HOME="${1:-$EDGE_HOME_DEFAULT}"
  local current="$EDGE_HOME/tools/current"
  local previous="$EDGE_HOME/tools/previous"
  # 只切 tools/current ↔ tools/previous；绝不触碰 data-plane current/previous 或任何服务。
  if [ ! -L "$current" ]; then
    echo "TOOLS_RELEASE_ROLLBACK=FAIL（tools/current 不存在）" >&2
    exit 1
  fi
  if [ ! -L "$previous" ]; then
    echo "TOOLS_RELEASE_ROLLBACK=FAIL（tools/previous 不存在，无法回滚）" >&2
    exit 1
  fi
  local cur_name prev_name
  cur_name="$(readlink "$current")"
  prev_name="$(readlink "$previous")"
  # 目标 previous 必须仍完整。
  local prev_sha
  prev_sha="$(basename "$prev_name")"
  verify_release "$EDGE_HOME/tools/releases/$prev_sha" "$prev_sha" || {
    echo "TOOLS_RELEASE_ROLLBACK=FAIL（previous release 完整性校验失败）" >&2
    exit 1
  }
  # 原子交换 current/previous（不触碰 data-plane）。
  # new current = old previous（prev_name）；new previous = old current（cur_name）。
  local tmp_cur="$EDGE_HOME/tools/.current.tmp.$$"
  ln -s "$prev_name" "$tmp_cur"
  rm -f "$previous"
  ln -s "$cur_name" "$previous"
  mv -Tf "$tmp_cur" "$current"
  [ "$(readlink "$EDGE_HOME/tools/current")" = "$prev_name" ] || {
    echo "TOOLS_RELEASE_ROLLBACK=FAIL（current 回滚后校验失败）" >&2
    exit 1
  }
  echo "TOOLS_RELEASE_ROLLBACK=PASS"
  echo "TOOLS_CURRENT_SHA=$prev_sha"
  echo "TOOLS_PREVIOUS_SHA=$(basename "$cur_name")"
  echo "MANAGEMENT_PLANE_ROLLBACK=PASS"
  echo "DATA_PLANE_UNCHANGED=YES"
}

cmd_status() {
  EDGE_HOME="${1:-$EDGE_HOME_DEFAULT}"
  local cur="none" prev="none" manifest_valid="NO" checksum_valid="NO"
  if [ -L "$EDGE_HOME/tools/current" ]; then
    cur="$(basename "$(readlink "$EDGE_HOME/tools/current")")"
  fi
  if [ -L "$EDGE_HOME/tools/previous" ]; then
    prev="$(basename "$(readlink "$EDGE_HOME/tools/previous")")"
  fi
  if [ "$cur" != "none" ]; then
    local release="$EDGE_HOME/tools/releases/$cur"
    if grep -q "\"source_sha\": \"$cur\"" "$release/manifest.json" 2>/dev/null; then
      manifest_valid="YES"
    fi
    if (cd "$release" && sha256sum -c checksums.sha256 --quiet >/dev/null 2>&1); then
      checksum_valid="YES"
    fi
  fi
  echo "TOOLS_CURRENT_SHA=$cur"
  echo "TOOLS_PREVIOUS_SHA=$prev"
  echo "TOOLS_MANIFEST_VALID=$manifest_valid"
  echo "TOOLS_CHECKSUM_VALID=$checksum_valid"
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  switch) shift; cmd_switch "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  status) shift; cmd_status "$@" ;;
  *) echo "用法: $0 build <SOURCE_DIR> <SHA> [EDGE_HOME] | verify <SHA> | switch <SHA> | rollback | status" >&2; exit 2 ;;
esac
