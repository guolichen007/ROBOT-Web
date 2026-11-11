#!/usr/bin/env bash
# G1-R1 隔离 systemd 安装验收（真实 systemd 245、真实低权限 UID/GID、瞬态单元）。
#
# 授权范围（已获用户确认）：
#   - 创建/复用专用测试用户 firebotreadonlytest（--system，无 shell）；
#   - systemd-run 瞬态单元（不写 /etc/systemd/system，不触碰任何生产 unit）；
#   - 测试安装树与 broker 全部位于 $BASE（默认工作空间内 sysd-acceptance/）。
# 验证项：
#   1) 非 root systemd 服务可读本机专属凭证（0640 root:服务组），其他普通用户不可读；
#   2) Agent 不能写入凭证；3) WorkingDirectory/PYTHONPATH/venv/依赖锁定生效；
#   4) 首次启动成功（真实 mosquitto 收到 status）；5) 第二实例被拒（owner 守卫）；
#   6) systemctl stop 后再次启动成功（重启语义）；7) Restart=on-failure 策略生效；
#   8) 正常退出码 0；9) 清理瞬态单元。
# 不连接任何生产 Broker；不修改/启动生产旧 Bridge。
set -euo pipefail

TEST_USER="${READONLY_SYSD_TEST_USER:-firebotreadonlytest}"
BASE="${READONLY_SYSD_TEST_BASE:-$(cd "$(dirname "$0")/.." && pwd)/sysd-acceptance}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="firebot-readonly-acceptance"
VEHICLE="firebot-vehicle-01"

PASS_COUNT=0
step() { echo "--- $1"; }
ok() { echo "PASS  $1"; PASS_COUNT=$((PASS_COUNT + 1)); }

cleanup() {
  ${SUDO:-sudo} systemctl stop "$UNIT" 2>/dev/null || true
  ${SUDO:-sudo} systemctl reset-failed "$UNIT" 2>/dev/null || true
  if [ -f "$BASE/broker.pid" ]; then
    timeout 5 ${SUDO:-sudo} kill "$(cat "$BASE/broker.pid")" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ---------- 0. 准备测试用户、安装树、venv、凭证、证书、broker ----------
step "0. 准备隔离验收环境（测试用户/安装树/venv/凭证/证书/本地 broker）"
${SUDO:-sudo} useradd --system --no-create-home --shell /usr/sbin/nologin "$TEST_USER" 2>/dev/null || true
${SUDO:-sudo} mkdir -p "$BASE"/{repo,run,etc,certs,logs}
${SUDO:-sudo} cp -a "$REPO/services" "$BASE/repo/services" 2>/dev/null || true
if [ ! -x "$BASE/venv/bin/python3" ]; then
  ${SUDO:-sudo} "${PYTHON3_11:-python3.11}" -m venv "$BASE/venv"
  ${SUDO:-sudo} "$BASE/venv/bin/pip" install -q --no-input \
    -r "$REPO/services/vehicle_agent_v1_readonly/requirements.txt"
fi
"$BASE/venv/bin/python3" -c \
  'import paho.mqtt, pydantic; assert paho.mqtt.__version__=="2.1.0" and pydantic.VERSION=="2.11.7"' \
  && ok "venv python3.11 + 依赖锁定 (paho 2.1.0 / pydantic 2.11.7)"

# 测试证书（TEST ONLY，本机 127.0.0.1 broker）
${SUDO:-sudo} chown "$(id -u):$(id -g)" "$BASE/certs" 2>/dev/null || true
if [ ! -f "$BASE/certs/ca.crt" ]; then
  bash "$REPO/scripts/generate_readonly_test_certs.sh" "$BASE/certs" >/dev/null
fi

# 凭证文件：root:测试用户 0640（正式方案同构）
${SUDO:-sudo} tee "$BASE/etc/device_identity.json" >/dev/null <<EOF
{
  "vehicle_id": "firebot-vehicle-01",
  "display_name": "firebot-001",
  "mqtt_host": "127.0.0.1",
  "mqtt_port": ${READONLY_SYSD_BROKER_PORT:-18999},
  "mqtt_username": "firebot-vehicle-01",
  "mqtt_password": "acceptance-only-vehicle-password",
  "ca_cert_path": "$BASE/certs/ca.crt"
}
EOF
${SUDO:-sudo} chown root:"$TEST_USER" "$BASE/etc/device_identity.json"
${SUDO:-sudo} chmod 0640 "$BASE/etc/device_identity.json"
${SUDO:-sudo} chown -R root:"$TEST_USER" "$BASE/repo"
${SUDO:-sudo} chown "$TEST_USER":"$TEST_USER" "$BASE/run"
${SUDO:-sudo} chmod 0700 "$BASE/run"

# 本地测试 broker（127.0.0.1，仅验收用）
PORT="${READONLY_SYSD_BROKER_PORT:-18999}"
${SUDO:-sudo} rm -f "$BASE/passwd" && ${SUDO:-sudo} touch "$BASE/passwd"
${SUDO:-sudo} mosquitto_passwd -b "$BASE/passwd" platform acceptance-platform-password
${SUDO:-sudo} mosquitto_passwd -b "$BASE/passwd" firebot-vehicle-01 acceptance-only-vehicle-password
${SUDO:-sudo} tee "$BASE/broker.conf" >/dev/null <<EOF
listener $PORT 127.0.0.1
allow_anonymous false
password_file $BASE/passwd
cafile $BASE/certs/ca.crt
certfile $BASE/certs/server.crt
keyfile $BASE/certs/server.key
require_certificate false
persistence false
EOF
${SUDO:-sudo} chown "$(id -u):$(id -g)" "$BASE/logs" 2>/dev/null || true
${SUDO:-sudo} bash -c 'echo $$ > "$1"; exec mosquitto -c "$2"' _ "$BASE/broker.pid" "$BASE/broker.conf" >>"$BASE/logs/broker.log" 2>&1 &
sleep 1
if ! ${SUDO:-sudo} kill -0 "$(cat "$BASE/broker.pid" 2>/dev/null)" 2>/dev/null; then
  echo "FAIL  本地 broker 未启动"; exit 1
fi
ok "本地验收 broker 启动 (127.0.0.1:$PORT, TLS)"

# ---------- 1. 凭证权限：非 root 服务用户可读、其他用户不可读、Agent 不可写 ----------
step "1. 凭证访问权限（非 root 读取 / 他人不可读 / 不可写）"
if ${SUDO:-sudo} runuser -u "$TEST_USER" -- test -r "$BASE/etc/device_identity.json"; then
  ok "服务用户 $TEST_USER 可读凭证（非 root 读取）"
else
  echo "FAIL  服务用户不可读凭证"; exit 1
fi
if ${SUDO:-sudo} runuser -u nobody -- cat "$BASE/etc/device_identity.json" >/dev/null 2>&1; then
  echo "FAIL  其他普通用户(nobody)可读凭证"; exit 1
else
  ok "其他普通用户(nobody)不可读凭证"
fi
if ${SUDO:-sudo} runuser -u "$TEST_USER" -- test -w "$BASE/etc/device_identity.json" 2>/dev/null; then
  echo "FAIL  Agent 可写凭证文件"; exit 1
else
  ok "Agent（服务用户）不能写入/更换凭证"
fi

# ---------- 2. 首次启动：systemd 瞬态单元，低权限 + WorkingDirectory + PYTHONPATH ----------
step "2. 首次启动（systemd-run 瞬态单元，User/Group=测试用户）"
${SUDO:-sudo} systemd-run --unit="$UNIT" --collect \
  --uid="$TEST_USER" --gid="$TEST_USER" \
  --working-directory="$BASE/repo" \
  --property="Environment=PYTHONPATH=$BASE/repo" \
  --property="Restart=no" \
  "$BASE/venv/bin/python3" -m services.vehicle_agent_v1_readonly.main \
    --config "$BASE/etc/device_identity.json" \
    --run-dir "$BASE/run" \
    --status-interval 1.5 --telemetry-interval 2.5
# 运行目录与 lock（生产同构：/var/lib 式独立目录，服务用户 0700）
for _ in $(seq 1 30); do
  ${SUDO:-sudo} runuser -u "$TEST_USER" -- test -f "$BASE/run/agent.lock" && break
  sleep 1
done
if ${SUDO:-sudo} runuser -u "$TEST_USER" -- test -f "$BASE/run/agent.lock"; then
  ok "run-dir 生效（服务用户可写、lock 已建；0700 隔离他人访问）"
else
  echo "FAIL  run-dir 未生效"; exit 1
fi
# WorkingDirectory 语义：直接核对主进程 cwd
MAIN_PID=$(systemctl show "$UNIT" -p MainPID --value)
if [ -n "$MAIN_PID" ] && [ "$MAIN_PID" != "0" ] \
   && [ "$(${SUDO:-sudo} readlink /proc/$MAIN_PID/cwd 2>/dev/null)" = "$BASE/repo" ]; then
  ok "WorkingDirectory 生效（主进程 cwd=$BASE/repo）"
else
  echo "FAIL  WorkingDirectory 未生效（MainPID=$MAIN_PID）"; exit 1
fi
# 真实 broker 观察：platform 账号订阅收到 status（QoS1 retained）
if timeout 30 mosquitto_sub -h 127.0.0.1 -p "$PORT" --cafile "$BASE/certs/ca.crt" \
     -u platform -P acceptance-platform-password \
     -t "v1/robot/$VEHICLE/status" -C 1 -W 25 2>/dev/null | grep -q '"online": true'; then
  ok "首次启动成功：真实 broker 收到 status（online=true）"
else
  echo "FAIL  首次启动未收到 status"; exit 1
fi

# ---------- 3. 第二实例被拒（运行期防双开） ----------
step "3. 第二实例被拒绝（owner 守卫）"
set +e
${SUDO:-sudo} runuser -u "$TEST_USER" -- env PYTHONPATH="$BASE/repo" \
  "$BASE/venv/bin/python3" -m services.vehicle_agent_v1_readonly.main \
    --config "$BASE/etc/device_identity.json" \
    --run-dir "$BASE/run" >/dev/null 2>&1
SECOND_RC=$?
set -e
if [ "$SECOND_RC" = "3" ]; then
  ok "第二实例退出码 3（owner 冲突拒绝）"
else
  echo "FAIL  第二实例退出码 $SECOND_RC（期望 3）"; exit 1
fi

# ---------- 4. 正常停止与再次启动（重启语义） ----------
step "4. systemctl stop 后再次启动"
${SUDO:-sudo} systemctl stop "$UNIT"
sleep 1
if systemctl is-active "$UNIT" >/dev/null 2>&1; then
  echo "FAIL  stop 后单元仍 active"; exit 1
fi
ok "systemctl stop 生效（瞬态单元停止）"
${SUDO:-sudo} systemd-run --unit="$UNIT" --collect \
  --uid="$TEST_USER" --gid="$TEST_USER" \
  --working-directory="$BASE/repo" \
  --property="Environment=PYTHONPATH=$BASE/repo" \
  --property="Restart=no" \
  "$BASE/venv/bin/python3" -m services.vehicle_agent_v1_readonly.main \
    --config "$BASE/etc/device_identity.json" \
    --run-dir "$BASE/run" --status-interval 1.5 --telemetry-interval 2.5
if timeout 30 mosquitto_sub -h 127.0.0.1 -p "$PORT" --cafile "$BASE/certs/ca.crt" \
     -u platform -P acceptance-platform-password \
     -t "v1/robot/$VEHICLE/status" -C 1 -W 25 2>/dev/null | grep -q '"online": true'; then
  ok "再次启动成功（stop→start 重启语义，lock 已释放）"
else
  echo "FAIL  重启后未收到 status"; exit 1
fi

# ---------- 5. Restart=on-failure 策略与正常退出码 ----------
step "5. Restart=on-failure 策略（占用 lock 制造冲突 → systemd 自动重启）"
${SUDO:-sudo} systemctl stop "$UNIT"; sleep 1
# 先由另一个进程持有 flock，模拟冲突：Agent 启动即退出 3，systemd 应自动重启
${SUDO:-sudo} flock "$BASE/run/agent.lock" -c "sleep 25" &
HOLDER=$!
sleep 0.5
${SUDO:-sudo} systemd-run --unit="$UNIT" --collect \
  --uid="$TEST_USER" --gid="$TEST_USER" \
  --working-directory="$BASE/repo" \
  --property="Environment=PYTHONPATH=$BASE/repo" \
  --property="Restart=on-failure" --property="RestartSec=1" \
  "$BASE/venv/bin/python3" -m services.vehicle_agent_v1_readonly.main \
    --config "$BASE/etc/device_identity.json" \
    --run-dir "$BASE/run" --status-interval 1.5 --telemetry-interval 2.5
sleep 6
NR=$(systemctl show "$UNIT" -p NRestarts --value)
if [ "${NR:-0}" -ge 1 ]; then
  ok "Restart=on-failure 生效（NRestarts=$NR，冲突退出 3 被 systemd 重启）"
else
  echo "FAIL  Restart=on-failure 未生效（NRestarts=$NR）"; exit 1
fi
${SUDO:-sudo} systemctl stop "$UNIT"
wait "$HOLDER" 2>/dev/null || true

# ---------- 6. 正常退出码 0（SIGTERM 优雅退出，服务用户身份前台验证） ----------
step "6. 正常退出码（SIGTERM → 0）"
${SUDO:-sudo} runuser -u "$TEST_USER" -- env PYTHONPATH="$BASE/repo" \
  "$BASE/venv/bin/python3" -m services.vehicle_agent_v1_readonly.main \
    --config "$BASE/etc/device_identity.json" \
    --run-dir "$BASE/run" \
    --status-interval 1.5 --telemetry-interval 2.5 >/dev/null 2>"$BASE/logs/agent-exit.log" &
AGENT_PID=$!
sleep 3
# 父子链定位 python 主进程（pgrep -P 不匹配命令行，避免自匹配/误伤调用方）：
# sudo(AGENT_PID) -> runuser -> python
RUNUSER_PID=$(pgrep -P "$AGENT_PID" | head -1)
PY_PID=$(pgrep -P "${RUNUSER_PID:-0}" | head -1)
if [ -n "$PY_PID" ]; then
  ${SUDO:-sudo} kill -TERM "$PY_PID"
else
  echo "FAIL  未定位到 Agent 主进程（AGENT_PID=$AGENT_PID RUNUSER_PID=${RUNUSER_PID:-?}）"
  ${SUDO:-sudo} kill -TERM "$AGENT_PID" 2>/dev/null || true
fi
wait "$AGENT_PID" || AGENT_RC=$?
AGENT_RC=${AGENT_RC:-0}
if [ "$AGENT_RC" = "0" ]; then
  ok "正常退出码 0（SIGTERM 优雅退出）"
else
  echo "FAIL  Agent 退出码 $AGENT_RC（期望 0）"; exit 1
fi

# ---------- 7. 清理 ----------
step "7. 清理瞬态单元"
${SUDO:-sudo} systemctl stop "$UNIT" 2>/dev/null || true
${SUDO:-sudo} systemctl reset-failed "$UNIT" 2>/dev/null || true
ok "瞬态单元已清理（测试用户 $TEST_USER 保留，安装树位于 $BASE）"
echo "== SYSTEMD ACCEPTANCE PASSED（$PASS_COUNT 项）=="
