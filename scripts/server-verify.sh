#!/usr/bin/env bash
# Firebot SERVER 验收脚本：全部用容器内部检查，不依赖宿主机端口/NOAUTH 猜测。
set -uo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

# 容器健康判定统一走 docker inspect（compose ps -q → cid → State.Running + State.Health.Status），
# 不依赖容器 Name 文本（旧写法 grep " api " 匹配不到 firebot-server-api-1 这类 compose 名）。
get_service_container_id() {
  "${COMPOSE[@]}" ps -q "$1" 2>/dev/null | head -1
}

service_healthy() {
  local cid
  cid="$(get_service_container_id "$1")"
  [ -n "$cid" ] || return 1
  docker inspect --format '{{.State.Running}} {{.State.Health.Status}}' "$cid" 2>/dev/null | grep -q '^true healthy$'
}

# --self-test：验证 service_healthy 解析逻辑（不依赖真实 docker/compose）
if [ "${1:-}" = "--self-test" ]; then
  _mock_cid=""
  _mock_health=""
  get_service_container_id() { echo "$_mock_cid"; }
  docker() {
    [ "$1" = "inspect" ] || return 1
    case "$_mock_health" in
      healthy)   echo "true healthy" ;;
      unhealthy) echo "true unhealthy" ;;
      stopped)   echo "false" ;;
      *)         echo "" ;;
    esac
  }
  _fail=0
  _mock_cid="abc123"; _mock_health="healthy"
  if service_healthy api; then echo "SELF_TEST running+healthy=PASS"; else echo "SELF_TEST running+healthy=FAIL"; _fail=1; fi
  _mock_cid="abc123"; _mock_health="unhealthy"
  if service_healthy api; then echo "SELF_TEST running+unhealthy=FAIL"; _fail=1; else echo "SELF_TEST running+unhealthy=PASS"; fi
  _mock_cid="";       _mock_health="healthy"
  if service_healthy api; then echo "SELF_TEST missing-container=FAIL"; _fail=1; else echo "SELF_TEST missing-container=PASS"; fi
  _mock_cid="abc123"; _mock_health="stopped"
  if service_healthy api; then echo "SELF_TEST not-running=FAIL"; _fail=1; else echo "SELF_TEST not-running=PASS"; fi
  exit $_fail
fi

echo "== Firebot SERVER verify =="
echo "SERVER_SHA=$(git rev-parse HEAD)"

# 1) 容器健康（compose health）
if "${COMPOSE[@]}" ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null | grep -vqE 'exited|unhealthy'; then
  echo "CONTAINERS_HEALTHY=PASS"
else
  echo "CONTAINERS_HEALTHY=CHECK"
fi

# 2) API 容器内部 health（127.0.0.1:8000，不经宿主机 80/443）
if "${COMPOSE[@]}" exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)" 2>/dev/null; then
  echo "API_HEALTH=PASS"
else
  echo "API_HEALTH=FAIL"
fi

# 3) MQTT 8883（容器端口映射，而非宿主机 ss）
if "${COMPOSE[@]}" port mosquitto 8883 >/dev/null 2>&1; then
  echo "MQTT_8883=PASS"
else
  echo "MQTT_8883=FAIL"
fi

# 4) ingress / dispatcher 心跳（走 api 容器内 get_redis，自动用 REDIS_URL 认证，不会 NOAUTH 误判）
if "${COMPOSE[@]}" exec -T api python -c \
  "from app.core.events import get_redis; raise SystemExit(0 if get_redis().get('service:mqtt-ingress:heartbeat') else 1)" 2>/dev/null; then
  echo "MQTT_INGRESS_HEALTH=PASS"
else
  echo "MQTT_INGRESS_HEALTH=FAIL"
fi
if "${COMPOSE[@]}" exec -T api python -c \
  "from app.core.events import get_redis; r=get_redis(); raise SystemExit(0 if r.get('service:command-dispatcher:outbox-heartbeat') and r.get('service:command-dispatcher:safety-heartbeat') else 1)" 2>/dev/null; then
  echo "COMMAND_DISPATCHER_HEALTH=PASS"
else
  echo "COMMAND_DISPATCHER_HEALTH=FAIL"
fi

# 5) schema 文件存在
[ -f packages/protocol-schemas/firebot-message-1.2.schema.json ] && echo "SCHEMA_1_2=PRESENT" || echo "SCHEMA_1_2=MISSING"
[ -f packages/protocol-schemas/firebot-message-1.3.schema.json ] && echo "SCHEMA_1_3=PRESENT" || echo "SCHEMA_1_3=MISSING"

# 6) 车辆身份 + 实时投影 + 控制 flags（DB 权威，容器内查询；不硬编码车辆 ID）
VEHICLE_ID="${FIREBOT_VERIFY_VEHICLE_ID:-}"
case "$VEHICLE_ID" in
  "")
    echo "ROBOT=UNKNOWN（未提供 FIREBOT_VERIFY_VEHICLE_ID，fail-closed，不查默认车辆）"
    ;;
  *[!A-Za-z0-9_-]*)
    echo "ROBOT=UNKNOWN（FIREBOT_VERIFY_VEHICLE_ID 含非法字符）"
    ;;
  *)
    "${COMPOSE[@]}" exec -T api python -c \
      "import json
from app.db.session import SessionLocal
from app.db.models import Robot, RobotCapability, RobotDataChannel, RobotIntegrationProfile
from app.modules.commands.readiness import robot_readiness
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == '$VEHICLE_ID'))
p = db.get(RobotIntegrationProfile, r.id) if r else None
cap = db.get(RobotCapability, r.id) if r else None
channels = db.scalars(select(RobotDataChannel).where(RobotDataChannel.robot_id == r.id)).all() if r else []
readiness = robot_readiness(db, r) if r else {}
print(json.dumps({
  'vehicle_id': '$VEHICLE_ID',
  'exists': r is not None,
  'enabled': r.enabled if r else None,
  'online_state': r.online_state if r else None,
  'current_mode': r.current_mode if r else None,
  'estop_active': r.estop_active if r else None,
  'battery': r.battery if r else None,
  'boot_id': r.boot_id if r else None,
  'last_seen_at': str(r.last_seen_at) if r and r.last_seen_at else None,
  'source_kind': p.source_kind if p else None,
  'control_contract_verified': p.control_contract_verified if p else None,
  'ack_contract_verified': p.ack_contract_verified if p else None,
  'map_contract_verified': p.map_contract_verified if p else None,
  'bidirectional_bridge_verified': p.bidirectional_bridge_verified if p else None,
  'command_path_verified': p.command_path_verified if p else None,
  'cmd_vel_arbitration_verified': p.cmd_vel_arbitration_verified if p else None,
  'capabilities': cap.supported_commands_json if cap else None,
  'data_channels': [c.channel for c in channels],
  'stop_ready': bool(readiness.get('safety_command_ready', {}).get('stop_motion')),
  'patrol_ready': bool(readiness.get('autonomous_task_ready', {}).get('patrol')),
  'readiness_reasons': readiness.get('readiness_reasons', []),
}))" 2>/dev/null || echo "ROBOT $VEHICLE_ID=UNKNOWN（DB 查询失败）"
    ;;
esac

# 7) 控制能力分层（代码实现 ≠ 现场开放 ≠ 实车验收）
echo "CONTROL_CODE=PATROL_START,STOP_MOTION"
echo "CONTROL_FIELD_VERIFIED=NO"
echo "ROS_COMPAT_DOWNLINK=NOT_IMPLEMENTED"

# 8) 镜像来源版本（OCI revision label，必须与批准 SHA 一致）
VERIFY_SHA="${VERIFY_SHA:-}"
image_revision() {
  local cid
  cid="$(get_service_container_id "$1")"
  [ -n "$cid" ] || { echo "UNKNOWN"; return; }
  docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid" 2>/dev/null || echo "UNKNOWN"
}
API_IMAGE_REVISION="$(image_revision api)"
INGRESS_IMAGE_REVISION="$(image_revision mqtt-ingress)"
DISPATCHER_IMAGE_REVISION="$(image_revision command-dispatcher)"
WORKER_IMAGE_REVISION="$(image_revision task-worker)"
WEB_IMAGE_REVISION="$(image_revision web)"
echo "API_IMAGE_REVISION=$API_IMAGE_REVISION"
echo "INGRESS_IMAGE_REVISION=$INGRESS_IMAGE_REVISION"
echo "DISPATCHER_IMAGE_REVISION=$DISPATCHER_IMAGE_REVISION"
echo "WORKER_IMAGE_REVISION=$WORKER_IMAGE_REVISION"
echo "WEB_IMAGE_REVISION=$WEB_IMAGE_REVISION"
if [ -n "$VERIFY_SHA" ]; then
  mismatch=""
  for rev in "$API_IMAGE_REVISION" "$INGRESS_IMAGE_REVISION" "$DISPATCHER_IMAGE_REVISION" "$WORKER_IMAGE_REVISION" "$WEB_IMAGE_REVISION"; do
    [ "$rev" = "$VERIFY_SHA" ] || mismatch="$mismatch $rev"
  done
  if [ -n "$mismatch" ]; then
    echo "IMAGE_SHA_MATCH=FAIL（期望 $VERIFY_SHA，异常:$mismatch）"
    exit 1
  else
    echo "IMAGE_SHA_MATCH=PASS"
  fi
fi

# 9) task-worker 心跳（J6-S1 生产必需）
if "${COMPOSE[@]}" exec -T api python -c \
  "from app.core.events import get_redis; raise SystemExit(0 if get_redis().get('service:task-worker:heartbeat') else 1)" 2>/dev/null; then
  echo "TASK_WORKER_HEARTBEAT=PASS"
else
  echo "TASK_WORKER_HEARTBEAT=FAIL"
  exit 1
fi

# 10) required services 全部 healthy（fail-closed：任一 unhealthy 即 exit != 0）
for svc in api mqtt-ingress command-dispatcher task-worker web; do
  if service_healthy "$svc"; then
    echo "SERVICE_${svc}=HEALTHY"
  else
    echo "SERVICE_${svc}=UNHEALTHY"
    exit 1
  fi
done

# ros-compat-adapter：ROS_COMPAT_MODE=true 才 required；默认 false 下 OPTIONAL，缺失/不健康不 fail。
ROS_COMPAT_MODE="${ROS_COMPAT_MODE:-false}"
if [ "$ROS_COMPAT_MODE" = "true" ]; then
  if service_healthy ros-compat-adapter; then
    echo "SERVICE_ros-compat-adapter=HEALTHY"
  else
    echo "SERVICE_ros-compat-adapter=UNHEALTHY"
    exit 1
  fi
else
  echo "SERVICE_ros-compat-adapter=OPTIONAL（ROS_COMPAT_MODE=false）"
fi
