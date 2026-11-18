#!/usr/bin/env bash
# build-libsrt.sh — 受控构建 libsrt 1.4.4（媒体组件 SRT 发送端依赖）。
#
# 为什么需要：Ubuntu 20.04 发行版 libsrt1 / srt-tools 为 1.4.0；云端 MediaMTX 1.18（gosrt）
# 要求对端 SRT ≥ 1.4.1，1.4.0 握手必被 REJ_VERSION 拒绝（实测）。
#
# 来源钉死：官方 GitHub release tag v1.4.4 源码包 + SHA256；构建于车端本机（Ubuntu 20.04 的
# gcc-9 / cmake / libssl-dev），只产出共享库，不安装任何二进制到系统路径。
#
# 用法（root 或任意用户；输出目录由调用方安装进 Edge Home media/lib/）：
#   build-libsrt.sh <OUT_DIR> [--source <tar.gz>]      # 无 --source 时下载钉死 URL
# 离线：先在有网机器下载同一源码包，--source 指定；SHA256 不符一律拒绝。
set -euo pipefail

SRT_VERSION=1.4.4
SRT_URL="https://github.com/Haivision/srt/archive/refs/tags/v${SRT_VERSION}.tar.gz"
SRT_SHA256=93f5f3715bd5bd522b8d65fc0d086ef2ad49db6a41ad2d7b35df2e8bd7094114

OUT="${1:-}"; shift || true
SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SRC="${2:-}"; shift 2 ;;
    *) echo "LIBSRT_BUILD=FAIL 未知参数 $1" >&2; exit 2 ;;
  esac
done
[ -n "$OUT" ] || { echo "用法: $0 <OUT_DIR> [--source <tar.gz>]" >&2; exit 2; }
CMAKE="${FIREBOT_CMAKE:-cmake}"
for tool in "$CMAKE" make sha256sum tar; do
  command -v "$tool" >/dev/null 2>&1 || { echo "LIBSRT_BUILD=FAIL 缺少 $tool（apt install cmake build-essential libssl-dev）" >&2; exit 1; }
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/firebot-libsrt.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
if [ -z "$SRC" ]; then
  SRC="$WORK/srt-${SRT_VERSION}.tar.gz"
  curl -fsSL --retry 3 -o "$SRC" "$SRT_URL" || { echo "LIBSRT_BUILD=FAIL 下载失败（可用 --source 离线包）" >&2; exit 1; }
fi
actual="$(sha256sum "$SRC" | awk '{print $1}')"
[ "$actual" = "$SRT_SHA256" ] || { echo "LIBSRT_BUILD=FAIL 源码包 SHA256 不符（期望 $SRT_SHA256）" >&2; exit 1; }

tar -xzf "$SRC" -C "$WORK"
mkdir -p "$WORK/build"
"$CMAKE" -S "$WORK/srt-${SRT_VERSION}" -B "$WORK/build" \
  -DCMAKE_BUILD_TYPE=Release -DENABLE_APPS=OFF -DENABLE_STATIC=OFF -DENABLE_SHARED=ON \
  -DUSE_ENCLIB=openssl ${FIREBOT_LIBSRT_CMAKE_ARGS:-} >"$WORK/cmake.log" 2>&1 \
  || { tail -20 "$WORK/cmake.log" >&2; echo "LIBSRT_BUILD=FAIL cmake（需 libssl-dev）" >&2; exit 1; }
grep -q "ENCRYPTION: ENABLED" "$WORK/cmake.log" || { echo "LIBSRT_BUILD=FAIL 未启用加密（SRT passphrase 需要）" >&2; exit 1; }
make -C "$WORK/build" -j"$(nproc 2>/dev/null || echo 2)" srt_shared >"$WORK/make.log" 2>&1 \
  || { tail -20 "$WORK/make.log" >&2; echo "LIBSRT_BUILD=FAIL make" >&2; exit 1; }

mkdir -p "$OUT"
cp -a "$WORK/build/libsrt.so.${SRT_VERSION}" "$OUT/"
ln -sfn "libsrt.so.${SRT_VERSION}" "$OUT/libsrt.so.1.4"
cp "$WORK/srt-${SRT_VERSION}/LICENSE" "$OUT/LICENSE.libsrt.MPL-2.0"
sha256sum "$OUT/libsrt.so.${SRT_VERSION}" | awk '{print $1}' > "$OUT/libsrt.so.${SRT_VERSION}.sha256"
printf 'LIBSRT_VERSION=%s\nLIBSRT_SOURCE_SHA256=%s\n' "$SRT_VERSION" "$SRT_SHA256" > "$OUT/LIBSRT.env"
chmod 0644 "$OUT/libsrt.so.${SRT_VERSION}" "$OUT/LICENSE.libsrt.MPL-2.0" "$OUT/LIBSRT.env" "$OUT/libsrt.so.${SRT_VERSION}.sha256"
echo "LIBSRT_BUILD=PASS version=$SRT_VERSION out=$OUT"
