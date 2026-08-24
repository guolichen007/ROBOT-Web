#!/usr/bin/env bash
# Firebot SERVER 验收脚本：全部用容器内部检查，不依赖宿主机端口/NOAUTH 猜测。
set -uo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

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

# 6) firebot-vehicle-01 身份 + 实时投影 + 控制 flags（DB 权威，容器内查询）
"${COMPOSE[@]}" exec -T api python -c \
  "import json
from app.db.session import SessionLocal
from app.db.models import Robot, RobotIntegrationProfile
from sqlalchemy import select
db = SessionLocal()
r = db.scalar(select(Robot).where(Robot.vehicle_id == 'firebot-vehicle-01'))
p = db.get(RobotIntegrationProfile, r.id) if r else None
print(json.dumps({
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
}))" 2>/dev/null || echo "ROBOT firebot-vehicle-01=UNKNOWN（DB 查询失败）"

echo "REAL_CONTROL=NOT_IMPLEMENTED"
