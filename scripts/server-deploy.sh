#!/usr/bin/env bash
# server-deploy.sh — SERVER 模块化部署（exact SHA + immutable image + manifest + rollback）
#
# 用法：
#   TARGET_SHA=<40SHA> ./server-deploy.sh preflight
#   TARGET_SHA=<40SHA> ./server-deploy.sh migrate          # 需 BACKUP_VERIFIED=1
#   TARGET_SHA=<40SHA> ./server-deploy.sh api|web|worker|control-plane|all-app
#   TARGET_SHA=<40SHA> ./server-deploy.sh verify
#   TARGET_SHA=<40SHA> ./server-deploy.sh rollback
#   TARGET_SHA=<40SHA> ./server-deploy.sh refresh-ros-compat # 同 SHA 维护，不改 SSOT
#   ./server-deploy.sh status                              # 读 current.json，不需 TARGET_SHA
#
# 服务分组：control-plane = api+mqtt-ingress+command-dispatcher+task-worker
# 绝不默认重启 postgres/redis/mosquitto/nginx/mediamtx。
# 版本 SSOT：/opt/firebot/deployments/current.json（current/previous runtime）
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)
TARGET_SHA="${TARGET_SHA:-}"
PY_IMAGE="firebot-python:${TARGET_SHA}"
WEB_IMAGE="firebot-web:${TARGET_SHA}"
DEPLOY_ROOT="/opt/firebot/deployments"
CURRENT_JSON="$DEPLOY_ROOT/current.json"
# Exact rollback may check out a historical revision without this new helper.
# Keep its code in the running process so SSOT can still be written afterwards.
DEPLOYMENT_STATE_CODE="$(cat scripts/deployment_state.py)"
deployment_state() { python3 -c "$DEPLOYMENT_STATE_CODE" "$@"; }

CONTROL_PLANE=(api mqtt-ingress command-dispatcher task-worker)
APP_ALL=(api mqtt-ingress command-dispatcher task-worker web)
source scripts/vehicle-agent-rollout.sh
init_deploy_context() {
  V1_REQUIRED="$(v1_required)" || { echo 'ERROR: cannot determine v1 rollout requirement' >&2; return 1; }
  if [ "$V1_REQUIRED" = true ]; then
    COMPOSE+=(--profile vehicle-agent)
    CONTROL_PLANE+=(vehicle-agent-adapter)
    APP_ALL+=(vehicle-agent-adapter)
  fi
}

# ---- 版本 SSOT ----
current_sha() {
  runtime_value "$(runtime_descriptor current)" sha
}
previous_sha() {
  runtime_value "$(runtime_descriptor previous)" sha
}
runtime_descriptor() {
  deployment_state read "$CURRENT_JSON" "$1"
}
runtime_value() {
  python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2],"UNKNOWN"))' "$1" "$2"
}
exact_runtime() {
  printf '{"kind":"exact_sha","sha":"%s"}\n' "$1"
}
write_ssot() {
  # $1=current descriptor $2=previous descriptor; provenance is never an exact SHA.
  deployment_state write "$CURRENT_JSON" "$1" "$2"
}
rollback_to_legacy_snapshot() {
  python3 scripts/legacy_runtime.py restore "$1"
}
restore_runtime() {
  case "$(runtime_value "$1" kind)" in
    legacy_snapshot) rollback_to_legacy_snapshot "$(runtime_value "$1" manifest)" ;;
    exact_sha) rollback_to "$(runtime_value "$1" sha)" ;;
    *) echo 'ROLLBACK_RUNTIME_KIND=FAIL' >&2; return 1 ;;
  esac
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
  local dirty
  dirty="$(git status --porcelain)"
  # exact-SHA release 必须完全 clean，包含所有 untracked/staged 文件。
  if [ -n "$dirty" ]; then
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

get_service_container_id() {
  "${COMPOSE[@]}" ps -q "$1" 2>/dev/null | head -1
}

service_healthy() {
  local cid
  cid="$(get_service_container_id "$1")"
  [ -n "$cid" ] || return 1
  docker inspect --format '{{.State.Running}} {{.State.Health.Status}}' "$cid" 2>/dev/null | grep -q '^true healthy$'
}

recreate() {
  local services=("$@")
  echo "  重建服务：${services[*]}"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" \
    "${COMPOSE[@]}" up -d --no-deps --wait "${services[@]}" || return 1
  # --wait 后检查 required services healthy（fail-closed，统一走 docker inspect）
  for svc in "${services[@]}"; do
    service_healthy "$svc" || { echo "ERROR: $svc 未 healthy" >&2; return 1; }
  done
}

compose_project_name() {
  "${COMPOSE[@]}" config --format json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'
}

rollback_service_set() {
  # Read the checked-out rollback target, including services behind profiles.
  local defined svc
  defined="$("${COMPOSE[@]}" --profile '*' config --services)" || return 1
  ROLLBACK_SERVICES=()
  for svc in api mqtt-ingress command-dispatcher task-worker web; do
    grep -Fxq "$svc" <<< "$defined" || { echo "ROLLBACK_SERVICE_MISSING=$svc" >&2; return 1; }
    ROLLBACK_SERVICES+=("$svc")
  done
  ROLLBACK_HAS_ADAPTER=false
  if grep -Fxq vehicle-agent-adapter <<< "$defined"; then
    ROLLBACK_SERVICES+=(vehicle-agent-adapter)
    ROLLBACK_HAS_ADAPTER=true
  fi
  echo "ROLLBACK_SERVICE_SET=${ROLLBACK_SERVICES[*]}"
}

rollback_adapter_ids() {
  # Project labels also find orphaned/stopped instances after the service is
  # removed from the old Compose file; never inspect or stop another project.
  docker ps -a -q --filter "label=com.docker.compose.project=$1" \
    --filter label=com.docker.compose.service=vehicle-agent-adapter
}

rollback_adapter_absent() {
  local remaining
  remaining="$(rollback_adapter_ids "$1")" || return 1
  [ -z "$remaining" ] || { echo 'ADAPTER_POST_ROLLBACK=FAIL_PRESENT' >&2; return 1; }
  echo 'ADAPTER_POST_ROLLBACK=ABSENT'
}

rollback_to() {
  # Shared by explicit rollback and failed-rollout recovery. No DB downgrade.
  local prev="$1" project target_project ids cid revision svc
  local adapter_ids=()
  [[ "$prev" =~ ^[0-9a-f]{40}$ ]] || return 1
  git cat-file -e "$prev:docker-compose.server.yml" || return 1
  project="$(compose_project_name)" || return 1
  [ -n "$project" ] || return 1
  ids="$(rollback_adapter_ids "$project")" || return 1
  if [ -n "$ids" ]; then
    mapfile -t adapter_ids <<< "$ids"
    # Stop/remove the new adapter BEFORE any old control-plane service starts.
    # A post-adapter target recreates its own exact-SHA adapter below.
    docker stop "${adapter_ids[@]}" || return 1
    docker rm "${adapter_ids[@]}" || return 1
  fi
  rollback_adapter_absent "$project" || return 1
  git checkout "$prev" || return 1
  [ "$(git rev-parse HEAD)" = "$prev" ] || return 1
  TARGET_SHA="$prev"
  PY_IMAGE="firebot-python:$prev"
  WEB_IMAGE="firebot-web:$prev"
  target_project="$(compose_project_name)" || return 1
  [ "$target_project" = "$project" ] || { echo 'ROLLBACK_PROJECT_MISMATCH=FAIL' >&2; return 1; }
  rollback_service_set || return 1
  APP_ALL=("${ROLLBACK_SERVICES[@]}")
  recreate "${ROLLBACK_SERVICES[@]}" || return 1
  for svc in "${ROLLBACK_SERVICES[@]}"; do
    cid="$(get_service_container_id "$svc")" || return 1
    revision="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid")" || return 1
    [ "$revision" = "$prev" ] || { echo "ROLLBACK_IMAGE_SHA_MATCH=FAIL service=$svc" >&2; return 1; }
  done
  if [ "$ROLLBACK_HAS_ADAPTER" = false ]; then
    rollback_adapter_absent "$project" || return 1
  else
    echo 'ADAPTER_POST_ROLLBACK=HEALTHY_PREVIOUS_SHA'
  fi
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
  python3 - "$TARGET_SHA" "$scope" "$prev" "$ts" "$dir" "$mig_before" "$mig_after" "$ENV_FILE" "$CURRENT_JSON" <<'PY'
import json, subprocess, sys
target_sha, scope, prev, ts, d, mig_before, mig_after, env_file = sys.argv[1:9]
with open(sys.argv[9], encoding="utf-8") as stream:
    runtime_state = json.load(stream)

def inspect(args):
    try:
        return subprocess.check_output(args, text=True).strip()
    except Exception:
        return ""

def cid_of(svc):
    return inspect(["docker", "compose", "--env-file", env_file, "-f", "docker-compose.server.yml", "ps", "-q", svc])

manifest = {
    "requested_sha": target_sha,
    "target_sha": runtime_state.get("current_sha"),
    "runtime_state": runtime_state,
    "branch": inspect(["git", "branch", "--show-current"]),
    "previous_sha": prev,
    "deploy_scope": scope,
    "timestamp": ts,
    "migration_before": mig_before,
    "migration_after": mig_after,
    "containers": {},
}
for svc in ("api", "mqtt-ingress", "command-dispatcher", "task-worker", "web", "ros-compat-adapter", "vehicle-agent-adapter"):
    cid = cid_of(svc)
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
  local current
  current="$(runtime_descriptor current)" || return 1
  echo "RUNTIME_KIND=$(runtime_value "$current" kind)"
  if [ "$(runtime_value "$current" kind)" = legacy_snapshot ]; then
    echo "current_git_provenance_sha=$(runtime_value "$current" git_provenance_sha)"
    echo "current_runtime_manifest=$(runtime_value "$current" manifest)"
  fi
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

migration_head() {
  # 从 TARGET_SHA 工作树的 Alembic migration 图计算 head（不读旧 API 容器，避免把旧代码 head 当目标 head）。
  python3 - <<'PY'
import re
from pathlib import Path

versions = Path("apps/api/alembic/versions")
revisions: dict[str, str | None] = {}
for p in sorted(versions.glob("2026*.py")):
    text = p.read_text(encoding="utf-8")
    rev = re.search(r'revision\s*=\s*["\']([^"\']+)["\']', text)
    down = re.search(r'down_revision\s*=\s*["\']([^"\']+)["\']', text)
    if rev:
        revisions[rev.group(1)] = down.group(1) if down else None
downs = {v for v in revisions.values() if v}
heads = [r for r in revisions if r not in downs]
print(heads[-1] if heads else "unknown")
PY
}

do_migration_needed() {
  require_sha
  local db head
  db="$(migration_revision)"
  head="$(migration_head)"
  if [ "$db" = "$head" ]; then
    echo "MIGRATION=SKIP（db=$db == head=$head）"
    return 0
  fi
  echo "MIGRATION=NEEDED（db=$db != head=$head）"
  return 1
}

do_backup() {
  # 真实 pg_dump 备份（gzip + sha256 + 0600 + manifest），禁止假 BACKUP_VERIFIED。
  require_sha
  local dir="$DEPLOY_ROOT/backups"
  mkdir -p "$dir"
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local file="$dir/firebot-$ts.sql.gz"
  "${COMPOSE[@]}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip' > "$file"
  [ -s "$file" ] || { echo "BACKUP=FAIL（备份文件为空）" >&2; exit 1; }
  chmod 600 "$file"
  local sum
  sum="$(sha256sum "$file" | cut -d' ' -f1)"
  printf '{"backup_file":"%s","sha256":"%s","timestamp":"%s","sha":"%s"}\n' \
    "$file" "$sum" "$ts" "$TARGET_SHA" > "$file.manifest.json"
  echo "BACKUP=PASS file=$file sha256=$sum"
}

deploy_scope() {
  local scope="$1"
  shift
  require_sha
  local prev prev_runtime prior_previous kind mig_before
  if [ "$V1_REQUIRED" = true ]; then
    v1_acl_gate || { echo 'ERROR: active broker v1 ACL gate failed' >&2; exit 1; }
  fi
  prev_runtime="$(runtime_descriptor current)" || return 1
  prior_previous="$(runtime_descriptor previous)" || return 1
  if [ -n "${FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST:-}" ]; then
    [ "$scope" = all-app ] || { echo 'ERROR: first clean cutover requires all-app' >&2; return 1; }
    python3 scripts/legacy_runtime.py validate "$FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST" || return 1
    prev_runtime="$(python3 scripts/legacy_runtime.py descriptor "$FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST")" || return 1
    deployment_state canary "$CURRENT_JSON" "$prev_runtime" || return 1
  elif [ "$(runtime_value "$prev_runtime" kind)" = legacy_snapshot ]; then
    echo 'ERROR: legacy cutover requires explicit FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST' >&2
    return 1
  fi
  kind="$(runtime_value "$prev_runtime" kind)"
  prev="$(runtime_value "$prev_runtime" sha)"
  # migration_before 必须在整个 server deploy 开始时就记录（firebotctl 传 DEPLOY_MIGRATION_BEFORE），
  # 不能等 migrate 之后才取（否则会误记 migration 后的 revision）。
  mig_before="${DEPLOY_MIGRATION_BEFORE:-$(migration_revision)}"
  if ! build_images "$@" || ! recreate "$@"; then
    echo "SERVER_DEPLOY=FAIL（rollout 失败，自动回滚 runtime=$kind）" >&2
    if [ "$kind" = legacy_snapshot ]; then
      echo 'AUTO_ROLLBACK_MODE=LEGACY_SNAPSHOT' >&2
      if restore_runtime "$prev_runtime" && write_ssot "$prev_runtime" "$prior_previous"; then
        echo 'AUTO_ROLLBACK_LEGACY=PASS' >&2
      else
        echo 'AUTO_ROLLBACK_LEGACY=FAIL' >&2
      fi
    elif [ "$prev" != "UNKNOWN" ]; then
      echo 'AUTO_ROLLBACK_MODE=EXACT_SHA' >&2
      if restore_runtime "$prev_runtime"; then
        echo "AUTO_ROLLBACK=PASS（回 $prev）" >&2
      else
        echo "AUTO_ROLLBACK=FAIL（高优先级报警，需人工介入）" >&2
      fi
    else
      echo "AUTO_ROLLBACK=FAIL（无 previous SHA）" >&2
    fi
    exit 1
  fi
  # 成功后才更新 SSOT；下一次 clean release 会自然记录 previous=exact_sha。
  write_ssot "$(exact_runtime "$TARGET_SHA")" "$prev_runtime" || return 1
  write_manifest "$scope" "$prev" "$mig_before" || return 1
  echo "DEPLOY=PASS scope=$scope sha=$TARGET_SHA previous=$prev"
}

do_verify() {
  # verify 不需要外部 TARGET_SHA：从 current.json 读期望 SHA
  local current expected
  current="$(runtime_descriptor current)" || return 1
  if [ "$(runtime_value "$current" kind)" = legacy_snapshot ]; then
    python3 scripts/legacy_runtime.py verify "$(runtime_value "$current" manifest)" || return 1
    echo 'SERVER_VERIFY=PASS runtime=legacy_snapshot'
    return 0
  fi
  expected="$(current_sha)"
  if [ "$expected" = "UNKNOWN" ]; then
    echo "SERVER_VERIFY=FAIL（无 current.json，先 deploy）" >&2
    exit 1
  fi
  # 传 VERIFY_SHA 给 server-verify.sh，且失败必须 exit != 0
  VERIFY_SHA="$expected" scripts/server-verify.sh
}

do_refresh_ros_compat() {
  require_sha
  local current before after cid revision
  current="$(runtime_descriptor current)" || return 1
  if [ "$(runtime_value "$current" kind)" != exact_sha ] || \
     [ "$(runtime_value "$current" sha)" != "$TARGET_SHA" ]; then
    echo 'ROS_COMPAT_REFRESH=FAIL (requires current exact_sha == TARGET_SHA)' >&2
    return 1
  fi
  before="$(sha256sum "$CURRENT_JSON" | cut -d' ' -f1)" || return 1
  # Maintenance never invokes deploy_scope/write_ssot or changes previous runtime.
  if ! build_images ros-compat-adapter || ! recreate ros-compat-adapter; then
    echo 'ROS_COMPAT_REFRESH=FAIL (build or health)' >&2
    return 1
  fi
  cid="$(get_service_container_id ros-compat-adapter)" || return 1
  revision="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid")" || return 1
  [ "$revision" = "$TARGET_SHA" ] || { echo 'ROS_COMPAT_REFRESH=FAIL (revision)' >&2; return 1; }
  after="$(sha256sum "$CURRENT_JSON" | cut -d' ' -f1)" || return 1
  [ "$before" = "$after" ] || { echo 'ROS_COMPAT_REFRESH=FAIL SSOT_CHANGED=YES' >&2; return 1; }
  echo 'ROS_COMPAT_REFRESH=PASS'
  echo 'SSOT_CHANGED=NO'
}

do_rollback() {
  local prev previous old_current kind mig_before
  previous="$(runtime_descriptor previous)" || return 1
  old_current="$(runtime_descriptor current)" || return 1
  kind="$(runtime_value "$previous" kind)"
  prev="$(runtime_value "$previous" sha)"
  if [ "$kind" = exact_sha ] && { [ "$prev" = UNKNOWN ] || [ "$prev" = "$(current_sha)" ]; }; then
    echo "ERROR: 没有可回滚的 previous SHA（$prev）" >&2
    exit 1
  fi
  echo "  回滚到 previous runtime: $kind"
  mig_before="$(migration_revision)"
  restore_runtime "$previous" || { echo 'ROLLBACK=FAIL' >&2; return 1; }
  write_ssot "$previous" "$old_current" || { echo 'ROLLBACK=FAIL (SSOT write failed)' >&2; return 1; }
  write_manifest rollback "$(runtime_value "$old_current" sha)" "$mig_before" || return 1
  echo "ROLLBACK=PASS runtime=$kind sha=$prev"
}

if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then
case "${1:-}" in
  status|verify|rollback|refresh-ros-compat) ;; # Do not guess rollout from the DB for recovery/maintenance.
  *) init_deploy_context ;;
esac
case "${1:-}" in
  status) do_status ;;
  preflight) do_preflight ;;
  migrate) do_migrate ;;
  backup) do_backup ;;
  migration-needed) do_migration_needed ;;
  current-revision) echo "$(migration_revision)" ;;
  api) deploy_scope "api" api ;;
  worker) deploy_scope "worker" task-worker ;;
  adapter)
    [ "$V1_REQUIRED" = true ] || { echo 'ERROR: set VEHICLE_AGENT_V1_ROLLOUT=true for first adapter rollout' >&2; exit 1; }
    deploy_scope "adapter" vehicle-agent-adapter ;;
  web) deploy_scope "web" web ;;
  control-plane) deploy_scope "control-plane" "${CONTROL_PLANE[@]}" ;;
  all-app) deploy_scope "all-app" "${APP_ALL[@]}" ;;
  verify) do_verify ;;
  refresh-ros-compat) do_refresh_ros_compat ;;
  rollback) do_rollback ;;
  *)
    echo "用法: TARGET_SHA=<40SHA> $0 status|preflight|migrate|api|worker|web|control-plane|all-app|verify|rollback|refresh-ros-compat" >&2
    exit 1
    ;;
esac
fi
