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

cid_of() { # compose ps 在并发重启瞬间可能瞬时空返回：带重试与容错
  local i out raw
  for i in 1 2 3; do
    raw="$("${FB[@]}" ps -q "$1" 2>&1 || true)"
    out="$(printf '%s\n' "$raw" | head -1)"
    [ -n "$out" ] && { printf '%s\n' "$out"; return 0; }
    echo "cid_of($1) empty attempt $i; raw=[$raw]" >&2
    sleep 2
  done
  return 0
}
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
# 宿主 curl 经工具沙箱对 127.0.0.1 高端口存在连接行为差异；以 ingress 容器内探测为准
INGRESS_NGINX_CID="$("${DOCKER[@]}" compose -p u-dao-ingress ps -q nginx 2>/dev/null | head -1 || true)"
wait_ingress_http() { # $1=url（http://127.0.0.1:18000/health/live 或 https://127.0.0.1:18443/health/live）
  local i
  for i in $(seq 1 40); do
    if [ -n "$INGRESS_NGINX_CID" ] && "${DOCKER[@]}" exec "$INGRESS_NGINX_CID" wget -q --no-check-certificate -O - "$1" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

mqtt_pub() { # $1=topic $2=payload [extra mosquitto_pub args...]
  "${DOCKER[@]}" run --rm --network host -v "$RT/secrets/mosquitto/certs:/certs:ro" "$MOSQ_IMG" \
    mosquitto_pub -V 5 -h 127.0.0.1 -p 18883 --cafile /certs/ca.crt \
    -u platform -P "$MQTT_PW" -t "$1" -m "$2" "${@:3}"
}
mqtt_sub() { # $1=topic $2=timeout_seconds [extra args...]
  "${DOCKER[@]}" run --rm --network host -v "$RT/secrets/mosquitto/certs:/certs:ro" "$MOSQ_IMG" \
    timeout "$2" mosquitto_sub -V 5 -h 127.0.0.1 -p 18883 --cafile /certs/ca.crt \
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

location_msg() { # $1=seq
  local mid
  mid="$(printf 'fa010000-0000-4000-8000-%012d' "$1")"   # message_id 必须为 UUID
  printf '{"schema_version":"1.3","message_id":"%s","type":"location","vehicle_id":"R001","boot_id":"%s","timestamp":"2026-10-27T10:00:00+08:00","seq":%s,"position":{"x":1.0,"y":2.0,"theta":0.5},"site_code":"U-DAO","map_code":"m1","map_version":"1","map_checksum":"0123456789abcdef","frame_id":"map"}' "$mid" "$BOOT" "$1"
}

DUR_TASK_MID="fa010000-0000-4000-8000-00000000d00d"
task_status_msg() { # durable 标记：task_status（未知任务在 handler 中正常返回，dedup 键仍会写入）
  printf '{"schema_version":"1.3","message_id":"%s","type":"task_status","vehicle_id":"R001","boot_id":"%s","timestamp":"2026-10-27T10:00:00+08:00","seq":900,"task_id":"fa010000-0000-4000-8000-00000000cafe","status":"executing","phase":"RUNNING","progress":10}' "$DUR_TASK_MID" "$BOOT"
}

# ---- Part A：基础栈故障注入 ----
step "前置检查"
gate "docker 可用" "${DOCKER[@]}" info
# 基线恢复：上一轮失败注入可能遗留停止的容器（自愈式起点）
"${FB[@]}" up -d --wait mqtt-ingress vehicle-agent-adapter mediamtx >/dev/null 2>&1 || true
gate "firebot 栈 api 健康" "${FB[@]}" exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)"
gate "测试车 R001 就绪" ensure_test_robot
# 故障注入消息使用固定测试 boot（UUID 格式，schema 要求；首条车端消息由 accept_boot_session 写入 robot.boot_id）
BOOT="fa010000-0000-4000-8000-000000000001"
echo "boot_id=$BOOT"

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
INGRESS_CID="$(cid_of mqtt-ingress)"
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
# 先建立 boot 会话（location 非 boot 建立类型；availability 属 realtime 组，必须在停机前送达）
mqtt_pub "robot/R001/availability" "{\"schema_version\":\"1.3\",\"message_id\":\"fa010000-0000-4000-8000-00000000b007\",\"type\":\"availability\",\"vehicle_id\":\"R001\",\"boot_id\":\"$BOOT\",\"timestamp\":\"2026-10-27T10:00:00+08:00\",\"seq\":1,\"state\":\"online\"}"
sleep 2
"${DOCKER[@]}" stop "$INGRESS_CID" >/dev/null 2>&1
sleep 2
for i in $(seq 2 51); do
  mqtt_pub "robot/R001/location" "$(location_msg "$i")"
done
# durable 队列要求 QoS1（QoS0 不排队）
mqtt_pub "robot/R001/task_status" "$(task_status_msg)" -q 1
"${DOCKER[@]}" start "$INGRESS_CID" >/dev/null 2>&1
gate "A8 ingress 心跳恢复" wait_heartbeat "service:mqtt-ingress:heartbeat" ingress
sleep 10
gate "A8 durable 队列恢复（断线期 task_status 被投递）" test "$(api_redis_get "dedup:message:$DUR_TASK_MID")" = "1"
gate "A8 realtime 无历史回放（断线期 location 未排队）" test "$(api_redis_get "dedup:message:fa010000-0000-4000-8000-000000000002")" = ""
mqtt_pub "robot/R001/location" "$(location_msg 52)"
sleep 8
gate "A8 最新 location 直达收敛（seq=52）" test "$(api_redis_get "seq:R001:${BOOT}:location")" = "52"

# ---- Part B：broker 级 MQTT 语义（Message Expiry / QoS0 / durable 对照）----
step "B1 Message Expiry：过期消息不补发（持久会话离线队列）"
SUB_ID="fault-sub-b1-$$"
# 建立持久会话订阅者后断开（-c 已隐含 MQTT5 会话持久化）
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID" -c >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"expired":"yes"}' -q 1 -D publish message-expiry-interval 2
sleep 5
OUT="$(mqtt_sub "robot/R001/command" 4 -q 1 -i "$SUB_ID" -c 2>/dev/null || true)"
gate "B1 过期命令未被补发" test -z "$OUT"
# 清理持久会话
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID" -c >/dev/null 2>&1 || true

step "B2 QoS0 不排队（manual 脉冲语义）"
SUB_ID2="fault-sub-b2-$$"
mqtt_sub "robot/R001/command" 3 -q 0 -i "$SUB_ID2" -c >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"manual_pulse":1}' -q 0
sleep 3
OUT2="$(mqtt_sub "robot/R001/command" 4 -q 0 -i "$SUB_ID2" -c 2>/dev/null || true)"
gate "B2 QoS0 断线不补发" test -z "$OUT2"

step "B3 durable 对照：未过期 QoS1 正常恢复投递"
SUB_ID3="fault-sub-b3-$$"
mqtt_sub "robot/R001/command" 3 -q 1 -i "$SUB_ID3" -c >/dev/null 2>&1 || true
mqtt_pub "robot/R001/command" '{"durable":"control"}' -q 1
sleep 2
OUT3="$(mqtt_sub "robot/R001/command" 4 -q 1 -i "$SUB_ID3" -c 2>/dev/null || true)"
gate "B3 未过期 QoS1 恢复投递（对照成功）" test -n "$OUT3"

# ---- Part C：shared ingress edge 恢复（R2-10）----
step "C1 firebot edge recreate → ingress 无人工 reload 恢复"
EDGE_CID="$(cid_of nginx)"
"${FB[@]}" up -d --force-recreate --no-deps nginx >/dev/null 2>&1
gate "C1 edge 重建后 healthy" wait_svc_healthy nginx
gate "C1 ingress 18000 health（无人工 reload）" wait_ingress_http "http://127.0.0.1:18000/health/live"
gate "C1 ingress 18443 health（bootstrap TLS）" wait_ingress_http "https://127.0.0.1:18443/health/live"

echo
echo "FAULT_INJECTION=PASS=$PASS FAIL=$FAILN"
[ "$FAILN" -eq 0 ]
