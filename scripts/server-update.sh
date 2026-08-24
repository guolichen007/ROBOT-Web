#!/usr/bin/env bash
# Firebot SERVER 更新脚本：从 Git 拉取 → preflight → build → migrate → 重建变更服务 → health。
#
# 绝不：覆盖 secret、重生成 MQTT 密码、重置数据库、force reset。
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

# 1) 工作区必须干净
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: 工作区不干净，先提交/暂存或清理再更新" >&2
  exit 1
fi

TARGET_SHA="$(git rev-parse HEAD)"
echo "TARGET_SHA=$TARGET_SHA"

# 2) preflight（secret/端口/依赖/环境）
SERVER_ENV_FILE="$ENV_FILE" scripts/server-preflight.sh

# 3) compose 配置校验
"${COMPOSE[@]}" config --quiet

# 4) build 变更镜像（server bridge path = mqtt-ingress + command-dispatcher + Mosquitto）
"${COMPOSE[@]}" build api mqtt-ingress command-dispatcher task-worker web

# 5) up -d --wait（内部会先跑 one-shot migrate：alembic upgrade + seed）
"${COMPOSE[@]}" up -d --wait

echo "SERVER_UPDATE_OK=YES sha=$TARGET_SHA"
