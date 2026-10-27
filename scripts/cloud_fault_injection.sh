#!/usr/bin/env bash
# U-Dao 云部署故障注入验收（R2-34 12 项 + R2-6/7/10 新用例）。
# 证明「短暂中断自动恢复」且不出现：重复执行 / 旧命令复活 / manual 重放 / ACK 错配 / 状态假成功。
# 前置：firebot 栈已部署（project u-dao-firebot）、shared ingress 运行（u-dao-ingress）。
# 用法：bash scripts/cloud_fault_injection.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/docker_cmd.sh"
docker_cmd_detect || exit 1

ENV_FILE="${SERVER_ENV_FILE:-/srv/u-dao/projects/firebot/runtime/env/.env.server}"
COMPOSE_FILES="${FIREBOT_COMPOSE_FILES:-docker-compose.server.yml deploy/u-dao/compose.u-dao.yml}"
FB=("${DOCKER[@]}" compose -p u-dao-firebot --env-file "$ENV_FILE")
for f in $COMPOSE_FILES; do FB+=(-f "$f"); done
RT="/srv/u-dao/projects/firebot/runtime"
CA="$RT/secrets/mosquitto/certs/ca.crt"
MQTT_PW="$(cat "$RT/secrets/app/mqtt_platform_password")"
MOSQ_IMG="eclipse-mosquitto:2.1.2-alpine"

PASS=0
FAILN=0
gate() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS $label"
    PASS=$((PASS + 1))
  else
    echo "FAIL $label"
    FAILN=$((FAILN + 1))
  fi
}
step() { echo; echo "== $1 =="; }

cid_of() { "${FB[@]}" ps -q "$1" | head -1; }
started_at() { "${DOCKER[@]}" inspect -f '{{.State.StartedAt}}' "$1"; }
api_redis_get() { # $1=key
  "${FB[@]}" exec -T api python -c \
    "from app.core.events import get_redis; print(get_redis().get('$1') or '')" 2>/dev/null
}
wait_heartbeat() { # $1=key $2=desc
  local i v
  for i in $(seq 1 45); do
    v="$(api_redis_get "$1" 2>/dev/null || true)"
    [ -n "$v" ] && return 0
    sleep 2
  done
  echo "heartbeat $2 ($1) did not recover in 90s" >&2
  return 1
}
wait_api_ready() {
  local i
  for i in $(seq 1 30); do
    if "${FB[@]}" exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}
wait_svc_healthy() { # $1=service
  local i cid
  for i in $(seq 1 30); do
    cid="$(cid_of "$1")"
    if [ -n "$cid" ] && "${DOCKER[@]}" inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null | grep -q healthy; then
      return 0
    fi
    sleep 2
  done
  return 1
}
wait_ingress_http() { # $1=url 前缀（http / -k https）
  local i
  for i in $(seq 1 40); do
    if curl -sf "$1" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

mqtt_pub() { # $1=topic $2=payload [extra mosquitto_pub args...]
  "${DOCKER[@]}" run --rm --network host "$MOSQ_IMG" \
    mosquitto_pub -V 5 -h 127.0.0.1 -p 18883 --cafile /dev/null --insecure \
    -u platform -P "$MQTT_PW" -t "$1" -m "$2" "${@:3}"
}
mqtt_sub() { # $1=topic $2=timeout_seconds [extra args...]
  "${DOCKER[@]}" run --rm --network host "$MOSQ_IMG" \
    timeout "$2" mosquitto_sub -V 5 -h 127.0.0.1 -p 18883 --cafile /dev/null --insecure \
    -u platform -P "$MQTT_PW" -t "$1" "${@:3}"
}

# ---- 0) 前置：测试车辆 R001（seed_demo=false 时无机器人；用应用模型创建最小规范车辆）----
ensure_test_robot() {
  "${FB[@]}" exec -T api python - <<'PY'
from app.db.session import SessionLocal
from app.db.models import Site, Robot, RobotCapability, RobotIntegrationProfile, StreamRegistry
from sqlalchemy import select
with SessionLocal.begin() as db:
    site = db.scalar(select(Site).where(Site.code == "U-DAO"))
    if not site:
        site = Site(code="U-DAO", name="U-Dao 测试站")
        db.add(site)
        db.flush()
    robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
    if not robot:
        robot = Robot(vehicle_id="R001", site_id=site.id, name="故障注入测试车 R001",
                      model="FIREBOT-FAULT-TEST", current_map_version="1")
        db.add(robot)
        db.flush()
    if not db.get(RobotCapability, robot.id):
        db.add(RobotCapability(robot_id=robot.id, protocol_version="1.3.0",
                               supported_commands_json=["manual_control", "stop_motion", "patrol"],
                               sensors_json=["smoke"], media_json=["roof_rgb", "roof_thermal"]))
    if not db.get(RobotIntegrationProfile, robot.id):
        db.add(RobotIntegrationProfile(robot_id=robot.id, source_kind="CANONICAL",
                                       upstream_protocol="canonical-mqtt-1.3"))
    for camera in ("roof_rgb", "roof_thermal"):
        exists = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == f"R001-{camera}"))
        if not exists:
            db.add(StreamRegistry(stream_id=f"R001-{camera}", robot_id=robot.id,
                                  camera_type=camera, provider="MEDIAMTX",
                                  playback_url=f"/media/R001-{camera}/whep",
                                  codec="H264", state="OFFLINE"))
print("TEST_ROBOT_READY")
PY
}

boot_id() {
  "${FB[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from app.db.models import Robot
from sqlalchemy import select
with SessionLocal() as db:
    r = db.scalar(select(Robot).where(Robot.vehicle_id == 'R001'))
    print(r.boot_id or 'NO_BOOT')
" 2>/dev/null | tr -d '\r'
}

location_msg() { # $1=seq $2=message_id $3=boot
  printf '{"schema_version":"1.3","message_id":"%s","type":"location","vehicle_id":"R001","boot_id":"%s","timestamp":"2026-10-27T10:00:00+08:00","seq":%s,"position":{"x":1.0,"y":2.0,"theta":0.5},"site_code":"U-DAO","map_code":"m1","map_version":"1","map_checksum":"0123456789abcdef","frame_id":"map"}' "$2" "$3" "$1"
}

alarm_msg() { # $1=message_id
  printf '{"schema_version":"1.3","message_id":"%s","type":"alarm","vehicle_id":"R001","boot_id":"%s","timestamp":"2026-10-27T10:00:00+08:00","seq":900,"fire_type":"test","event_id":"evt-%s","parking_slot_code":"NONE","confidence":0.5}' "$1" "$BOOT" "$1"
}

# ---- Part A：基础栈故障注入 ----
step "前置检查"
gate "docker 可用" "${DOCKER[@]}" info
gate "firebot 栈 api 健康" "${FB[@]}" exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)"
ensure_test_robot
BOOT="$(boot_id)"
echo "boot_id=$BOOT"
gate "测试车 R001 就绪" test "$BOOT" != "NO_BOOT"

step "A1 mqtt-ingress restart 后恢复（心跳+订阅）"
INGRESS_CID="$(cid_of mqtt-ingress)"
"${FB[@]}" restart mqtt-ingress >/dev/null 2>&1
gate "A1 ingress 心跳恢复" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress

step "A2 vehicle-agent-adapter restart 后恢复（若启用）"
ADAPTER_CID="$(cid_of vehicle-agent-adapter 2>/dev/null || true)"
if [ -n "$ADAPTER_CID" ]; then
  "${FB[@]}" restart vehicle-agent-adapter >/dev/null 2>&1
  gate "A2 adapter 心跳恢复" wait_heartbeat "service:vehicle-agent-adapter:heartbeat" adapter
else
  echo "SKIP A2（基础 runtime 未启用 vehicle-agent-adapter）"
fi

step "A3 Mosquitto restart：客户端同容器内自动恢复（不得重建容器）"
INGRESS_START="$(started_at "$INGRESS_CID")"
DISPATCHER_CID="$(cid_of command-dispatcher)"
DISPATCHER_START="$(started_at "$DISPATCHER_CID")"
"${FB[@]}" restart mosquitto >/dev/null 2>&1
gate "A3 ingress 心跳恢复（同容器）" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress
gate "A3 dispatcher 心跳恢复（同容器）" wait_heartbeat "service:command-dispatcher:outbox-heartbeat" dispatcher
gate "A3 ingress 容器未重建" test "$(started_at "$INGRESS_CID")" = "$INGRESS_START"
gate "A3 dispatcher 容器未重建" test "$(started_at "$DISPATCHER_CID")" = "$DISPATCHER_START"

step "A4 API restart 后恢复"
"${FB[@]}" restart api >/dev/null 2>&1
gate "A4 api /health/ready" wait_api_ready

step "A5 Redis restart 后心跳恢复"
"${FB[@]}" restart redis >/dev/null 2>&1
gate "A5 ingress 心跳恢复" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress
gate "A5 task-worker 心跳恢复" wait_heartbeat "service:task-worker:heartbeat" task-worker

step "A6 MediaMTX restart 后恢复"
"${FB[@]}" restart mediamtx >/dev/null 2>&1
gate "A6 mediamtx healthy" wait_svc_healthy mediamtx

step "A7 MQTT 短时断连（mosquitto pause 10s）后自动恢复"
"${DOCKER[@]}" pause "$(cid_of mosquitto)" >/dev/null 2>&1
sleep 10
"${DOCKER[@]}" unpause "$(cid_of mosquitto)" >/dev/null 2>&1
gate "A7 ingress 心跳恢复" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress
gate "A7 ingress 容器未重建" test "$(started_at "$INGRESS_CID")" = "$INGRESS_START"

step "A8 realtime 无 backlog 回放（R2-6）+ durable 短时恢复"
INGRESS_CID="$(cid_of mqtt-ingress)"
"${DOCKER[@]}" stop "$INGRESS_CID" >/dev/null 2>&1
sleep 2
for i in $(seq 1 50); do
  mqtt_pub "robot/R001/location" "$(location_msg "$i" "loc-$i" "$BOOT")"
done
mqtt_pub "robot/R001/alarm" "$(alarm_msg "dur-alarm-1")"
"${DOCKER[@]}" start "$INGRESS_CID" >/dev/null 2>&1
gate "A8 ingress 心跳恢复" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress
sleep 10
gate "A8 durable 队列恢复（断线期 alarm 被投递）" test "$(api_redis_get "dedup:message:dur-alarm-1")" = "1"
gate "A8 realtime 无历史回放（断线期 location 未排队）" test "$(api_redis_get "dedup:message:loc-1")" = ""
mqtt_pub "robot/R001/location" "$(location_msg 51 "loc-51" "$BOOT")"
sleep 8
gate "A8 最新 location 直达收敛（seq=51）" test "$(api_redis_get "seq:R001:${BOOT}:location")" = "51"

# ---- Part B：broker 级 MQTT 语义（Message Expiry / QoS0 / durable 对照）----
step "B1 Message Expiry：过期消息不补发（持久会话离线队列）"
SUB_ID="fault-sub-b1-$$"
# 建立持久会话订阅者后断开（--session-expiry-interval 600）
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID" -c --session-expiry-interval 600 >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"expired":"yes"}' -q 1 -D publish message-expiry-interval 2
sleep 5
OUT="$(mqtt_sub "robot/R001/command" 4 -q 1 -i "$SUB_ID" -c --session-expiry-interval 0 2>/dev/null || true)"
gate "B1 过期命令未被补发" test -z "$OUT"
# 清理持久会话
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID" -c --session-expiry-interval 0 >/dev/null 2>&1 || true

step "B2 QoS0 不排队（manual 脉冲语义）"
SUB_ID2="fault-sub-b2-$$"
mqtt_sub "robot/R001/command" 3 -q 0 -i "$SUB_ID2" -c --session-expiry-interval 600 >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"manual_pulse":1}' -q 0
sleep 3
OUT2="$(mqtt_sub "robot/R001/command" 4 -q 0 -i "$SUB_ID2" -c --session-expiry-interval 0 2>/dev/null || true)"
gate "B2 QoS0 断线不补发" test -z "$OUT2"

step "B3 durable 对照：未过期 QoS1 正常恢复投递"
SUB_ID3="fault-sub-b3-$$"
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID3" -c --session-expiry-interval 600 >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"durable":"control"}' -q 1
sleep 2
OUT3="$(mqtt_sub "robot/R001/command" 4 -q 1 -i "$SUB_ID3" -c --session-expiry-interval 0 2>/dev/null || true)"
gate "B3 未过期 QoS1 恢复投递（对照成功）" test -n "$OUT3"

# ---- Part C：shared ingress edge 恢复（R2-10）----
step "C1 firebot edge recreate → ingress 无人工 reload 恢复"
EDGE_CID="$(cid_of nginx)"
"${FB[@]}" up -d --force-recreate --no-deps nginx >/dev/null 2>&1
gate "C1 edge 重建后 healthy" wait_svc_healthy nginx
gate "C1 ingress 18000 health（无人工 reload）" wait_ingress_http "http://127.0.0.1:18000/health/live"
gate "C1 ingress 18443 health（bootstrap TLS）" wait_ingress_http "-k https://127.0.0.1:18443/health/live"

echo
echo "FAULT_INJECTION=PASS=$PASS FAIL=$FAILN"
[ "$FAILN" -eq 0 ]
