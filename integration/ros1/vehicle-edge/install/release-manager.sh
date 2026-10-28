#!/usr/bin/env bash
# release-manager.sh — Vehicle Edge release 组装与原子切换（规格十二/十三）。
#
# build：从 Git 交付源组装 releases/<SHA>/（VERSION + manifest.json + bin/ + bridge/ + lib/ + monitor/）
# switch：staging 完整性校验 → 原子 current/previous 切换（临时 symlink + mv；失败 current 不变）
#
# release 不包含 vehicle identity/secret（identity.env/mqtt.env/device-token 都在 releases 之外）。
# 禁止逐文件覆盖 current。
#
# 用法：
#   release-manager.sh build  <SOURCE_DIR> <SHA> [EDGE_HOME]
#   release-manager.sh switch <SHA> [EDGE_HOME]
set -euo pipefail

EDGE_HOME_DEFAULT="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SOURCE_DIR=""
SHA=""

manifest_json() {
  # manifest 由 lifecycle 自动生成（禁止人工长期维护）；不含任何 secret。
  printf '{\n  "product": "firebot-edge",\n  "schema_version": 1,\n  "source_sha": "%s"\n}\n' "$SHA"
}

cmd_build() {
  SOURCE_DIR="${1:?用法: release-manager.sh build <SOURCE_DIR> <SHA> [EDGE_HOME]}"
  SHA="${2:?}"
  EDGE_HOME="${3:-$EDGE_HOME_DEFAULT}"
  echo "$SHA" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: SHA 必须 40 位 hex" >&2; exit 1; }

  local release="$EDGE_HOME/releases/$SHA"
  local staging
  staging="$(mktemp -d "$EDGE_HOME/releases/.staging.XXXXXX")"

  # 0) 交付源车辆特定代码禁止（车辆只是数据；NO_VEHICLE_SPECIFIC_PRODUCTION_CODE）
  find "$SOURCE_DIR" -maxdepth 2 -type d \( -name 'R00[0-9]' -o -name 'R00[0-9]-*' \) | grep -q . && {
    echo "ERROR: 交付源含车辆特定目录（R00x），禁止组装（车辆只是数据）" >&2
    exit 1
  }

  # 1) 从交付源组装（车辆差异只能存在于配置，release 内禁止车辆特定代码/目录）
  mkdir -p "$staging/bin" "$staging/bridge" "$staging/lib" "$staging/monitor"
  cp -a "$SOURCE_DIR/VERSION" "$staging/VERSION"
  printf '%s\n' "$(manifest_json)" > "$staging/manifest.json"
  cp -a "$SOURCE_DIR/bin/." "$staging/bin/"
  cp -a "$SOURCE_DIR/bridge/." "$staging/bridge/"
  cp -a "$SOURCE_DIR/lib/." "$staging/lib/"
  cp -a "$SOURCE_DIR/monitor/." "$staging/monitor/"
  # 车辆特定代码禁止（合同：NO_VEHICLE_SPECIFIC_PRODUCTION_CODE）
  find "$staging" -maxdepth 2 -type d \( -name 'R00[0-9]' -o -name 'R00[0-9]-*' \) | grep -q . && {
    echo "ERROR: release 含车辆特定目录（R00x），禁止组装" >&2
    rm -rf "$staging"
    exit 1
  }

  # 2) 完整性校验：manifest source_sha == SHA；关键文件齐备
  grep -q "\"source_sha\": \"$SHA\"" "$staging/manifest.json" || {
    echo "ERROR: manifest source_sha 与 SHA 不一致" >&2
    rm -rf "$staging"
    exit 1
  }
  for required in VERSION manifest.json bin/firebot-edge bridge/run_bridge.sh; do
    [ -e "$staging/$required" ] || {
      echo "ERROR: release 缺少 $required" >&2
      rm -rf "$staging"
      exit 1
    }
  done

  # 3) 落位 releases/<SHA>（原子 rename；同名 SHA 已存在则拒绝覆盖——immutable）
  [ -e "$release" ] && { echo "ERROR: releases/$SHA 已存在（immutable，禁止覆盖）" >&2; rm -rf "$staging"; exit 1; }
  chmod -R u=rwX,go=rX "$staging"
  mv "$staging" "$release"
  echo "RELEASE_BUILD=PASS release=$release"
}

cmd_switch() {
  SHA="${1:?用法: release-manager.sh switch <SHA> [EDGE_HOME]}"
  EDGE_HOME="${2:-$EDGE_HOME_DEFAULT}"
  local release="$EDGE_HOME/releases/$SHA"
  [ -d "$release" ] || { echo "RELEASE_SWITCH=FAIL（releases/$SHA 不存在）" >&2; exit 1; }

  # 验证新 release 完整性后才切换
  grep -q "\"source_sha\": \"$SHA\"" "$release/manifest.json" 2>/dev/null || {
    echo "RELEASE_SWITCH=FAIL（manifest 校验失败）" >&2
    exit 1
  }
  [ -x "$release/bin/firebot-edge" ] || { echo "RELEASE_SWITCH=FAIL（bin/firebot-edge 不可执行）" >&2; exit 1; }

  # 原子 current/previous 切换：临时 symlink + mv（POSIX rename 语义）
  local old=""
  if [ -L "$EDGE_HOME/current" ]; then
    old="$(readlink "$EDGE_HOME/current")"
  fi
  local tmp_current="$EDGE_HOME/.current.tmp.$$"
  ln -s "releases/$SHA" "$tmp_current"
  if [ -n "$old" ] && [ -e "$EDGE_HOME/previous" ] || [ -L "$EDGE_HOME/previous" ]; then
    rm -f "$EDGE_HOME/previous"
  fi
  if [ -n "$old" ]; then
    ln -s "$old" "$EDGE_HOME/previous"
  fi
  mv -Tf "$tmp_current" "$EDGE_HOME/current"
  [ "$(readlink "$EDGE_HOME/current")" = "releases/$SHA" ] || {
    echo "RELEASE_SWITCH=FAIL（current 切换后校验失败）" >&2
    exit 1
  }
  if [ -n "$old" ]; then
    prev_name="$(basename "$old")"
  else
    prev_name="none"
  fi
  echo "RELEASE_SWITCH=PASS sha=$SHA previous=${prev_name}"
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  switch) shift; cmd_switch "$@" ;;
  *) echo "用法: $0 build <SOURCE_DIR> <SHA> [EDGE_HOME] | switch <SHA> [EDGE_HOME]" >&2; exit 2 ;;
esac
