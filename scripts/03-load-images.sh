#!/usr/bin/env sh
set -eu

archive="${1:-./firebot-base-images-linux-amd64.tar}"
checksums="$(dirname "$archive")/SHA256SUMS.txt"
[ -f "$archive" ] || { echo "找不到镜像归档：$archive" >&2; exit 1; }
if [ -f "$checksums" ]; then
  (cd "$(dirname "$archive")" && sha256sum -c "$(basename "$checksums")")
fi
docker load -i "$archive"
docker image inspect \
  python:3.12.13-alpine3.24 node:24.19.0-alpine3.24 nginx:1.30.4-alpine \
  busybox:1.37.0-musl postgres:18.4-alpine redis:8.4.5-alpine \
  eclipse-mosquitto:2.1.2-alpine bluenviron/mediamtx:1.18.2 >/dev/null
echo "固定版本镜像已加载并验证。"
