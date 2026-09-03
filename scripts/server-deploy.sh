#!/usr/bin/env bash
# server-deploy.sh — SERVER 模块化部署（exact SHA + immutable image + manifest + rollback）
#
# 用法：
#   TARGET_SHA=<40SHA> ./server-deploy.sh preflight
#   TARGET_SHA=<40SHA> ./server-deploy.sh migrate          # 需 BACKUP_VERIFIED=1
#   TARGET_SHA=<40SHA> ./server-deploy.sh api|web|worker|control-plane|all-app
#   TARGET_SHA=<40SHA> ./server-deploy.sh verify
#   TARGET_SHA=<40SHA> ./server-deploy.sh rollback
#   ./server-deploy.sh status                              # 读 current.json，不需 TARGET_SHA
#
# 服务分组：control-plane = api+mqtt-ingress+command-dispatcher+task-worker
# 绝不默认重启 postgres/redis/mosquitto/nginx/mediamtx。
# 版本 SSOT：/opt/firebot/deployments/current.json（current/previous SHA）
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)
TARGET_SHA="${TARGET_SHA:-}"
PY_IMAGE="firebot-python:${TARGET_SHA}"
WEB_IMAGE="firebot-web:${TARGET_SHA}"
DEPLOY_ROOT="/opt/firebot/deployments"
CURRENT_JSON="$DEPLOY_ROOT/current.json"

CONTROL_PLANE=(api mqtt-ingress command-dispatcher task-worker)
APP_ALL=(api mqtt-ingress command-dispatcher task-worker web)

# ---- 版本 SSOT ----
current_sha() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("current_sha","UNKNOWN"))' "$CURRENT_JSON" 2>/dev/null || echo "UNKNOWN"
}
previous_sha() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("previous_sha","UNKNOWN"))' "$CURRENT_JSON" 2>/dev/null || echo "UNKNOWN"
}
write_ssot() {
  # $1=current $2=previous
  mkdir -p "$DEPLOY_ROOT"
  printf '{"current_sha": "%s", "previous_sha": "%s"}\n' "$1" "$2" > "$CURRENT_JSON"
}

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
  if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: 工作区不干净（含 untracked/staged 变更），先提交/清理再部署" >&2
    exit 1
  fi
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
    "${COMPOSE[@]}" up -d --no-deps --wait "${services[@]}"
  # --wait 后检查 required services healthy（fail-closed）
  for svc in "${services[@]}"; do
    local st
    st="$("${COMPOSE[@]}" ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null | grep -E " ${svc}( |$)" | head -1 || true)"
    echo "$st" | grep -qE 'healthy' || { echo "ERROR: $svc 未 healthy（$st）" >&2; return 1; }
  done
}

migration_revision() {
  "${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from sqlalchemy import text
db = SessionLocal()
print(db.scalar(text('SELECT version_num FROM alembic_version')) or 'unknown')
" 2>/dev/null || echo "unknown"
}

write_manifest() {
  local scope="$1" prev="$2" mig_before="$3" ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local dir="$DEPLOY_ROOT/${ts}-${TARGET_SHA}"
  mkdir -p "$dir"
  local mig_after
  mig_after="$(migration_revision)"
  python3 - "$TARGET_SHA" "$scope" "$prev" "$ts" "$dir" "$mig_before" "$mig_after" <<'PY'
import json, subprocess, sys
target_sha, scope, prev, ts, d, mig_before, mig_after = sys.argv[1:8]

def inspect(args):
    try:
        return subprocess.check_output(args, text=True).strip()
    except Exception:
        return ""

manifest = {
    "target_sha": target_sha,
    "branch": inspect(["git", "branch", "--show-current"]),
    "previous_sha": prev,
    "deploy_scope": scope,
    "timestamp": ts,
    "migration_before": mig_before,
    "migration_after": mig_after,
    "containers": {},
}
for svc in ("api", "mqtt-ingress", "command-dispatcher", "task-worker", "web"):
    cid = inspect(["docker", "compose", "ps", "-q", svc])
    if cid:
        manifest["containers"][svc] = {
            "container_id": cid,
            "image_id": inspect(["docker", "inspect", "--format", "{{.Image}}", cid]),
            "image_revision": inspect(["docker", "inspect", "--format",
                                       '{{ index .Config.Labels "org.opencontainers.image.revision" }}', cid]),
            "started_at": inspect(["docker", "inspect", "--format", "{{.State.StartedAt}}", cid]),
        }
with open(d + "/manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"  manifest 已写入 {d}/manifest.json")
PY
}

do_status() {
  echo "== SERVER 部署状态 =="
  echo "current_sha=$(current_sha)"
  echo "previous_sha=$(previous_sha)"
  echo "git HEAD=$(git rev-parse HEAD)"
  "${COMPOSE[@]}" ps --format '{{.Name}} {{.Image}} {{.State}} {{.Health}}'
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
    echo "ERROR: 生产 migration 前必须 backup 且验证；确认后 BACKUP_VERIFIED=1 重跑" >&2
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
  local prev mig_before
  prev="$(current_sha)"          # 部署前 current → 变成 previous
  mig_before="$(migration_revision)"
  build_images "$@"
  recreate "$@"
  # 成功后才更新 SSOT：previous=旧 current，current=TARGET_SHA
  write_ssot "$TARGET_SHA" "$prev"
  write_manifest "$scope" "$prev" "$mig_before"
  echo "DEPLOY=PASS scope=$scope sha=$TARGET_SHA previous=$prev"
}

do_verify() {
  # verify 不需要外部 TARGET_SHA：从 current.json 读期望 SHA
  local expected
  expected="$(current_sha)"
  if [ "$expected" = "UNKNOWN" ]; then
    echo "SERVER_VERIFY=FAIL（无 current.json，先 deploy）" >&2
    exit 1
  fi
  # 传 VERIFY_SHA 给 server-verify.sh，且失败必须 exit != 0
  VERIFY_SHA="$expected" scripts/server-verify.sh
}

do_rollback() {
  local prev
  prev="$(previous_sha)"
  if [ "$prev" = "UNKNOWN" ] || [ "$prev" = "$(current_sha)" ]; then
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
