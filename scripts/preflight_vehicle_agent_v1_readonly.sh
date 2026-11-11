#!/usr/bin/env bash
# firebot-vehicle-01 只读 Agent 正式安装前 preflight（只读检查，fail-closed）。
# 按 R001 现场证据核对：旧服务 unit = firebot-bridge.service，
# 旧程序目录 = /opt/firebot/vehicle-bridge，旧配置目录 = /etc/firebot。
# 本脚本不做任何安装/启停/修改；任何检查失败输出 FAIL 并退出 1。
set -u

FAILED=0

check() {  # check <名称> <命令...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    FAILED=1
  fi
}

echo "== preflight: vehicle-agent-v1-readonly (firebot-vehicle-01) =="

# 1. 旧 unit 状态（现场证据：inactive/disabled；出现 active 即阻断，先停旧桥）
check "old unit firebot-bridge.service is NOT active" \
  bash -c 'systemctl is-active --quiet firebot-bridge.service && exit 1 || exit 0'

# 2. 旧 Bridge 进程不存在（进程名/路径匹配；Agent 运行期还会二次守卫）
check "no legacy vehicle-bridge process" \
  bash -c 'pgrep -af "vehicle-bridge" | grep -v pgrep >/dev/null && exit 1 || exit 0'

# 3. 新 Agent 无残留实例（run-dir lock 不被持有）
check "no readonly agent lock held" \
  bash -c 'RUN_DIR="${1:-/var/lib/firebot/vehicle-agent-v1-readonly}"; [ -f "$RUN_DIR/agent.lock" ] && exit 1 || exit 0' _ "${READONLY_RUN_DIR:-/var/lib/firebot/vehicle-agent-v1-readonly}"

# 4. 新 Agent 身份凭证文件受控（存在且权限 0600，组/他零权限）
check "identity file mode 0600" \
  bash -c 'IDENTITY="${1:-/etc/firebot/vehicle-agent-v1-readonly/device_identity.json}"; [ -f "$IDENTITY" ] && [ "$(stat -c %a "$IDENTITY")" = "600" ]' _ "${READONLY_IDENTITY:-/etc/firebot/vehicle-agent-v1-readonly/device_identity.json}"

# 5. 虚拟环境与依赖锁定就位（python3.11 + paho/pydantic 精确版本）
check "venv python3.11 present" \
  bash -c 'PY="${1:-/opt/firebot/vehicle-agent-v1-readonly/venv/bin/python3}"; "$PY" -c "import sys; assert sys.version_info[:2] == (3, 11)"' _ "${READONLY_VENV_PY:-/opt/firebot/vehicle-agent-v1-readonly/venv/bin/python3}"
check "venv deps pinned" \
  bash -c 'PY="${1:-/opt/firebot/vehicle-agent-v1-readonly/venv/bin/python3}"; "$PY" -c "import paho.mqtt, pydantic; assert paho.mqtt.__version__==\"2.1.0\" and pydantic.VERSION==\"2.11.7\""' _ "${READONLY_VENV_PY:-/opt/firebot/vehicle-agent-v1-readonly/venv/bin/python3}"

# 6. 生产 CA 与 broker 网络可达性说明（连通性检查由安装 SOP 的人工步骤完成，
#    此处只确认 CA 文件存在；本脚本不连接生产 broker）
check "production CA file present" \
  bash -c 'CA="${1:-/etc/firebot/vehicle-agent-v1-readonly/ca.crt}"; [ -f "$CA" ]' _ "${READONLY_CA:-/etc/firebot/vehicle-agent-v1-readonly/ca.crt}"

if [ "$FAILED" -ne 0 ]; then
  echo "== preflight FAILED：禁止安装，先处置上述 FAIL 项 =="
  exit 1
fi
echo "== preflight PASSED（不表示已安装；安装步骤按 SOP 人工执行）=="
exit 0
