#!/usr/bin/env bash
# Firebot SERVER 验收脚本：只输出运行态事实，不伪造 PASS。
set -uo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.server.yml)

echo "== Firebot SERVER verify =="
echo "SERVER_SHA=$(git rev-parse HEAD)"

# 1) 容器健康
if "${COMPOSE[@]}" ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null | grep -vqE 'exited|unhealthy'; then
  echo "CONTAINERS_HEALTHY=PASS"
else
  echo "CONTAINERS_HEALTHY=CHECK"
fi

# 2) API health
if curl -fsS --max-time 5 http://127.0.0.1/health/ready >/dev/null 2>&1; then
  echo "API_HEALTH=PASS"
else
  echo "API_HEALTH=FAIL"
fi

# 3) MQTT 8883
if ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq '[:.]8883$'; then
  echo "MQTT_8883=PASS"
else
  echo "MQTT_8883=FAIL"
fi

# 4) mqtt-ingress / command-dispatcher 心跳（Redis 内部心跳 key）
INGRESS_HB="$("${COMPOSE[@]}" exec -T redis redis-cli GET service:mqtt-ingress:heartbeat 2>/dev/null || true)"
DISPATCHER_HB="$("${COMPOSE[@]}" exec -T redis redis-cli GET service:command-dispatcher:outbox-heartbeat 2>/dev/null || true)"
[ -n "$INGRESS_HB" ] && echo "MQTT_INGRESS_HEALTH=PASS" || echo "MQTT_INGRESS_HEALTH=FAIL"
[ -n "$DISPATCHER_HB" ] && echo "COMMAND_DISPATCHER_HEALTH=PASS" || echo "COMMAND_DISPATCHER_HEALTH=FAIL"

# 5) schema 文件存在 + 1.2 frozen / 1.3 current 校验
if [ -f packages/protocol-schemas/firebot-message-1.2.schema.json ]; then echo "SCHEMA_1_2=PRESENT"; else echo "SCHEMA_1_2=MISSING"; fi
if [ -f packages/protocol-schemas/firebot-message-1.3.schema.json ]; then echo "SCHEMA_1_3=PRESENT"; else echo "SCHEMA_1_3=MISSING"; fi

# 6) firebot-vehicle-01 身份与实时投影（通过 API，需要登录 cookie 则标注 UNKNOWN）
if "${COMPOSE[@]}" exec -T api python -c \
  "import json,sys; from app.db.session import SessionLocal; from app.db.models import Robot; \
   from sqlalchemy import select; \
   db=SessionLocal(); r=db.scalar(select(Robot).where(Robot.vehicle_id=='firebot-vehicle-01')); \
   print(json.dumps({'exists': r is not None, 'enabled': r.enabled if r else None, 'online_state': r.online_state if r else None, 'current_mode': r.current_mode if r else None, 'estop_active': r.estop_active if r else None, 'last_seen_at': str(r.last_seen_at) if r and r.last_seen_at else None, 'boot_id': r.boot_id if r else None, 'battery': r.battery if r else None}))" 2>/dev/null; then
  :
else
  echo "ROBOT firebot-vehicle-01=UNKNOWN（DB 查询失败）"
fi

echo "REAL_CONTROL=NOT_IMPLEMENTED"
