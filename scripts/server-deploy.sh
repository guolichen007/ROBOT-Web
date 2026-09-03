#!/usr/bin/env bash
# server-deploy.sh — SERVER 模块化部署（exact SHA + immutable image + manifest + rollback）
#
# 用法（必须显式指定 TARGET_SHA，禁止无 SHA 部署）：
#   TARGET_SHA=<40SHA> ./server-deploy.sh status
#   TARGET_SHA=<40SHA> ./server-deploy.sh preflight
#   TARGET_SHA=<40SHA> ./server-deploy.sh migrate
#   TARGET_SHA=<40SHA> ./server-deploy.sh api | web | worker | control-plane | all-app
#   TARGET_SHA=<40SHA> ./server-deploy.sh verify
#   TARGET_SHA=<40SHA> ./server-deploy.sh rollback
#
# 服务分组：
#   control-plane = api + mqtt-ingress + command-dispatcher + task-worker
#   绝不默认重启 postgres / redis / mosquitto / nginx / mediamtx（除非对应配置明确变化）。
#   镜像使用不可变 SHA tag：firebot-python:<SHA> / firebot-web:<SHA>，并打 OCI revision label。
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)
TARGET_SHA="${TARGET_SHA:-}"
PY_IMAGE="firebot-python:${TARGET_SHA}"
WEB_IMAGE="firebot-web:${TARGET_SHA}"
DEPLOY_ROOT="/opt/firebot/deployments"
PREVIOUS_SHA_FILE="$DEPLOY_ROOT/.previous-sha"

CONTROL_PLANE=(api mqtt-ingress command-dispatcher task-worker)
APP_ALL=(api mqtt-ingress command-dispatcher task-worker web)
INFRA=(postgres redis mosquitto nginx mediamtx)

require_sha() {
  if [ -z "$TARGET_SHA" ] || ! echo "$TARGET_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    echo "ERROR: TARGET_SHA 必须是 40 位 hex（当前: ${TARGET_SHA:-<空>}）" >&2
    exit 1
  fi
  local head
  head="$(git rev-parse HEAD)"
  if [ "$head" != "$TARGET_SHA" ]; then
    echo "ERROR: git HEAD($head) != TARGET_SHA($TARGET_SHA)，禁止部署非精确版本" >&2
    exit 1
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: 工作区不干净（含 staged 变更），先提交/清理再部署" >&2
    exit 1
  fi
}

previous_sha() { cat "$PREVIOUS_SHA_FILE" 2>/dev/null || echo "UNKNOWN"; }

record_previous() {
  mkdir -p "$DEPLOY_ROOT"
  echo "$TARGET_SHA" > "$PREVIOUS_SHA_FILE"
}

build_images() {
  local services=("$@")
  echo "  构建不可变镜像（tag=$TARGET_SHA）：${services[*]}"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" \
    "${COMPOSE[@]}" build --build-arg SOURCE_REVISION="$TARGET_SHA" "${services[@]}"
}

recreate() {
  local services=("$@")
  echo "  重建服务：${services[*]}"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" \
    "${COMPOSE[@]}" up -d --no-deps "${services[@]}"
}

write_manifest() {
  local scope="$1" prev="$2" ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local dir="$DEPLOY_ROOT/${ts}-${TARGET_SHA}"
  mkdir -p "$dir"
  local manifest="$dir/manifest.json"
  python3 - "$TARGET_SHA" "$scope" "$prev" "$ts" "$dir" <<'PY'
import json, subprocess, sys
target_sha, scope, prev, ts, d = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def inspect(args):
    try:
        return subprocess.check_output(args, text=True).strip()
    except Exception:
        return ""

images = {}
for name, image in (("api", f"firebot-python:{target_sha}"), ("web", f"firebot-web:{target_sha}")):
    img_id = inspect(["docker", "inspect", "--format", "{{.Id}}", image])
    if img_id:
        images[name] = img_id

containers = {}
for svc in ("api", "mqtt-ingress", "command-dispatcher", "task-worker", "web"):
    cid = inspect(["docker", "compose", "ps", "-q", svc])
    if cid:
        containers[svc] = cid

manifest = {
    "target_sha": target_sha,
    "branch": inspect(["git", "branch", "--show-current"]),
    "previous_sha": prev,
    "deploy_scope": scope,
    "timestamp": ts,
    "image_ids": images,
    "container_ids": containers,
    "started_at": inspect(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]),
    "migration": "see deploy log / alembic version",
}
with open(d + "/manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"  manifest 已写入 {d}/manifest.json")
PY
}

do_status() {
  echo "== SERVER 部署状态 =="
  echo "TARGET_SHA=${TARGET_SHA:-<未设置>}"
  echo "git HEAD=$(git rev-parse HEAD)"
  echo "previous_sha=$(previous_sha)"
  "${COMPOSE[@]}" ps --format '{{.Name}} {{.Image}} {{.State}}'
}

do_preflight() {
  require_sha
  SERVER_ENV_FILE="$ENV_FILE" UPDATE_MODE=true scripts/server-preflight.sh
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" "${COMPOSE[@]}" config --quiet
  echo "PREFLIGHT=PASS sha=$TARGET_SHA"
}

do_migrate() {
  require_sha
  if [ "${BACKUP_VERIFIED:-0}" != "1" ]; then
    echo "ERROR: 生产 migration 前必须完成 backup 且验证通过；确认后以 BACKUP_VERIFIED=1 重跑" >&2
    exit 1
  fi
  build_images api
  echo "  执行 one-shot migrate（alembic upgrade head + seed）"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" "${COMPOSE[@]}" run --rm migrate
  echo "MIGRATE=PASS sha=$TARGET_SHA"
}

deploy_scope() {
  local scope="$1"
  shift
  require_sha
  local prev
  prev="$(previous_sha)"
  build_images "$@"
  recreate "$@"
  record_previous
  write_manifest "$scope" "$prev"
  echo "DEPLOY=PASS scope=$scope sha=$TARGET_SHA previous=$prev"
}

do_verify() {
  require_sha
  scripts/server-verify.sh
}

do_rollback() {
  require_sha
  local prev
  prev="$(previous_sha)"
  if [ "$prev" = "UNKNOWN" ] || [ "$prev" = "$TARGET_SHA" ]; then
    echo "ERROR: 没有可回滚的 previous SHA（$prev）" >&2
    exit 1
  fi
  echo "  回滚到 previous SHA: $prev"
  git checkout "$prev"
  TARGET_SHA="$prev" "$0" all-app
}

case "${1:-}" in
  status) do_status ;;
  preflight) do_preflight ;;
  migrate) do_migrate ;;
  api) deploy_scope "api" api ;;
  worker) deploy_scope "worker" task-worker ;;
  web) deploy_scope "web" web ;;
  control-plane) deploy_scope "control-plane" "${CONTROL_PLANE[@]}" ;;
  all-app) deploy_scope "all-app" "${APP_ALL[@]}" ;;
  verify) do_verify ;;
  rollback) do_rollback ;;
  *)
    echo "用法: TARGET_SHA=<40SHA> $0 status|preflight|migrate|api|worker|web|control-plane|all-app|verify|rollback" >&2
    exit 1
    ;;
esac
