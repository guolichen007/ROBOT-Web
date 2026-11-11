#!/usr/bin/env bash
# firebot-vehicle-01 只读 Agent 正式安装 preflight（fail-closed，只读检查）。
#
# 两个阶段（G1-R1 拆分，禁止把"文件尚未创建"当作安装前置成功条件）：
#   preinstall  —— 环境预检：旧 Bridge 状态/进程、磁盘、Python、身份冲突、网络与 CA 预检。
#                  不依赖任何安装产物；产物缺失只警告，只有真实冲突才失败。
#   prestart    —— 安装产物与运行态预检：服务用户/组、虚拟环境与依赖锁定、凭证权限与
#                  服务用户实际可读性、运行目录、systemd unit、单 owner（实际 flock 获取验证）。
#
# 用法：
#   preflight_vehicle_agent_v1_readonly.sh preinstall [ROOTFS_PREFIX]
#   preflight_vehicle_agent_v1_readonly.sh prestart  [ROOTFS_PREFIX]
# ROOTFS_PREFIX 供隔离验收环境使用（如 chroot/测试前缀），默认空。
#
# 本脚本不做任何安装/启停/修改；任何 FAIL 输出退出 1。旧服务现场证据：
# unit=firebot-bridge.service，程序目录=/opt/firebot/vehicle-bridge，配置=/etc/firebot。
set -u

ROOTFS="${2:-}"
SVC_USER="${READONLY_SVC_USER:-firebot}"
SVC_GROUP="${READONLY_SVC_GROUP:-firebot}"
IDENTITY_FILE="${READONLY_IDENTITY:-/etc/firebot/vehicle-agent-v1-readonly/device_identity.json}"
CA_FILE="${READONLY_CA:-/etc/firebot/vehicle-agent-v1-readonly/ca.crt}"
RUN_DIR="${READONLY_RUN_DIR:-/var/lib/firebot/vehicle-agent-v1-readonly}"
INSTALL_ROOT="${READONLY_INSTALL_ROOT:-/opt/firebot/vehicle-agent-v1-readonly}"
UNIT_NAME="firebot-vehicle-agent-v1-readonly.service"
BROKER_HOST="${READONLY_BROKER_HOST:-}"
BROKER_PORT="${READONLY_BROKER_PORT:-8883}"

FAILED=0
WARNINGS=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILED=1; }
warn() { echo "WARN  $1"; WARNINGS=1; }

summary() {
  if [ "$FAILED" -ne 0 ]; then
    echo "== $1 FAILED：禁止继续，先处置 FAIL 项 =="
    exit 1
  fi
  echo "== $1 PASSED（WARN=$WARNINGS）=="
}

preinstall() {
  echo "== preinstall: vehicle-agent-v1-readonly (firebot-vehicle-01) =="

  # 1. 旧 Bridge unit 状态：active 即阻断（先停旧桥，再装新 Agent）
  if systemctl is-active --quiet "$ROOTFS"firebot-bridge.service 2>/dev/null; then
    fail "old unit firebot-bridge.service is ACTIVE (must be stopped/disabled first)"
  else
    pass "old unit firebot-bridge.service is not active"
  fi

  # 2. 旧 Bridge 进程不存在（进程名/路径匹配）
  if pgrep -af "vehicle-bridge" 2>/dev/null | grep -v pgrep | grep -q .; then
    fail "legacy vehicle-bridge process detected"
  else
    pass "no legacy vehicle-bridge process"
  fi

  # 3. 磁盘空间：目标分区至少 500MB 可用（venv+产物+日志余量）
  if ! df -Pk "${ROOTFS}/opt/firebot" 2>/dev/null | awk 'NR==2 {exit !($4 >= 512000)}'; then
    fail "disk space below 500MB on ${ROOTFS}/opt/firebot"
  else
    pass "disk space sufficient"
  fi

  # 4. Python 3.11 可用（版本精确到次版本，安装步骤将创建 venv）
  if ! command -v python3.11 >/dev/null 2>&1 || \
     ! python3.11 -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)' 2>/dev/null; then
    fail "python3.11 not available"
  else
    pass "python3.11 available"
  fi

  # 5. 身份冲突：旧身份环境变量必须不存在（新 Agent 会拒绝启动，这里提前暴露）
  if [ -n "${FIREBOT_VEHICLE_ID:-}" ] || [ -n "${BRIDGE_VEHICLE_ID:-}" ]; then
    fail "legacy identity env vars present (FIREBOT_VEHICLE_ID/BRIDGE_VEHICLE_ID)"
  else
    pass "no legacy identity env conflicts"
  fi

  # 6. CA 预检：文件未交付 → WARN（不允许把交付物缺失当成 preinstall 失败）；
  #    已交付但不可读 → FAIL。
  if [ ! -f "${ROOTFS}${CA_FILE}" ]; then
    warn "CA file not yet delivered (${CA_FILE}); required before PRESTART"
  elif [ ! -r "${ROOTFS}${CA_FILE}" ]; then
    fail "CA file unreadable: ${CA_FILE}"
  else
    pass "CA file delivered and readable"
  fi

  # 7. 网络预检：broker 连通性为 WARN（网络可能分段，不作为阻断项）；不可达必须在
  #    PRESTART 前人工确认。
  if [ -n "$BROKER_HOST" ]; then
    if timeout 5 bash -c "cat < /dev/null > /dev/tcp/${BROKER_HOST}/${BROKER_PORT}" 2>/dev/null; then
      pass "broker ${BROKER_HOST}:${BROKER_PORT} reachable"
    else
      warn "broker ${BROKER_HOST}:${BROKER_PORT} not reachable from this host (confirm network before PRESTART)"
    fi
  else
    warn "READONLY_BROKER_HOST not set; broker reachability not probed"
  fi

  summary "preinstall"
}

prestart() {
  echo "== prestart: vehicle-agent-v1-readonly (firebot-vehicle-01) =="

  # 1. 服务用户/组已存在（安装步骤必须先建用户再设权限；未建 → FAIL，禁止跳步）
  if ! getent passwd "$SVC_USER" >/dev/null 2>&1; then
    fail "service user '$SVC_USER' missing (create user/group before setting permissions)"
  else
    pass "service user '$SVC_USER' exists"
  fi
  if ! getent group "$SVC_GROUP" >/dev/null 2>&1; then
    fail "service group '$SVC_GROUP' missing"
  else
    pass "service group '$SVC_GROUP' exists"
  fi

  # 2. 虚拟环境与依赖锁定
  VENV_PY="${ROOTFS}${INSTALL_ROOT}/venv/bin/python3"
  if [ ! -x "$VENV_PY" ]; then
    fail "venv python missing: $VENV_PY"
  elif ! "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)' 2>/dev/null; then
    fail "venv python is not 3.11"
  elif ! "$VENV_PY" -c 'import paho.mqtt, pydantic; assert paho.mqtt.__version__=="2.1.0" and pydantic.VERSION=="2.11.7"' 2>/dev/null; then
    fail "venv deps not pinned (paho-mqtt==2.1.0, pydantic==2.11.7)"
  else
    pass "venv python3.11 with pinned deps"
  fi

  # 3. 凭证文件权限与属主：root:服务组、0640 或 0600；**并以服务用户实际读取验证**
  ID="${ROOTFS}${IDENTITY_FILE}"
  if [ ! -f "$ID" ]; then
    fail "identity file missing: $IDENTITY_FILE"
  else
    MODE=$(stat -c %a "$ID" 2>/dev/null)
    OWNER=$(stat -c %U:%G "$ID" 2>/dev/null)
    if [ "$OWNER" != "root:$SVC_GROUP" ] && [ "$OWNER" != "root:root" ]; then
      fail "identity file owner must be root:$SVC_GROUP (got $OWNER)"
    elif [ "$MODE" != "640" ] && [ "$MODE" != "600" ]; then
      fail "identity file mode must be 640/600 (got $MODE)"
    elif ! runuser -u "$SVC_USER" -- test -r "$ID" 2>/dev/null; then
      fail "service user '$SVC_USER' cannot read identity file (non-root read requirement)"
    elif [ "$MODE" = "640" ] && runuser -u "$SVC_USER" -- test -w "$ID" 2>/dev/null; then
      fail "service user '$SVC_USER' can WRITE identity file (must be read-only to agent)"
    else
      pass "identity file root:$SVC_GROUP 0$MODE, readable by service user, not writable by agent"
    fi
  fi

  # 4. 运行目录：属主服务用户、0700
  if [ ! -d "${ROOTFS}${RUN_DIR}" ]; then
    fail "run dir missing: $RUN_DIR"
  elif [ "$(stat -c %U:%G "${ROOTFS}${RUN_DIR}" 2>/dev/null)" != "$SVC_USER:$SVC_GROUP" ]; then
    fail "run dir owner must be $SVC_USER:$SVC_GROUP"
  elif [ "$(stat -c %a "${ROOTFS}${RUN_DIR}" 2>/dev/null)" != "700" ]; then
    fail "run dir mode must be 700"
  else
    pass "run dir owned by service user, mode 700"
  fi

  # 5. systemd unit 存在且语法有效（systemd-analyze verify）
  UNIT="${ROOTFS}/etc/systemd/system/${UNIT_NAME}"
  if [ ! -f "$UNIT" ]; then
    fail "systemd unit missing: $UNIT"
  elif ! systemd-analyze verify "$UNIT" >/dev/null 2>&1; then
    fail "systemd unit failed systemd-analyze verify"
  else
    pass "systemd unit present and verified"
  fi

  # 6. 单 owner：实际 flock 获取验证（文件存在 ≠ 锁被持有）
  LOCK="${ROOTFS}${RUN_DIR}/agent.lock"
  if [ -f "$LOCK" ]; then
    if ! flock -n "$LOCK" -c true 2>/dev/null; then
      fail "agent.lock is currently HELD by another instance"
    else
      pass "agent.lock not held (flock acquisition succeeded)"
    fi
  else
    pass "no agent.lock yet (first start; guard uses real flock)"
  fi

  # 7. 旧 Bridge 进程/unit 复查（PRESTART 再次确认）
  if pgrep -af "vehicle-bridge" 2>/dev/null | grep -v pgrep | grep -q .; then
    fail "legacy vehicle-bridge process appeared since preinstall"
  else
    pass "no legacy vehicle-bridge process"
  fi
  if systemctl is-active --quiet "${ROOTFS}"firebot-bridge.service 2>/dev/null; then
    fail "old unit firebot-bridge.service is ACTIVE"
  else
    pass "old unit firebot-bridge.service still not active"
  fi

  summary "prestart"
}

case "${1:-}" in
  preinstall) preinstall ;;
  prestart) prestart ;;
  *) echo "usage: $0 {preinstall|prestart} [ROOTFS_PREFIX]" >&2; exit 2 ;;
esac
