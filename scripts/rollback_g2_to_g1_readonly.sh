#!/usr/bin/env bash
# G2 → G1 readonly 受控回滚（001 车端）。
# 触发条件：G2 切换后任何异常（Ping/ACK 失败、ROS 反馈异常、msg_id 关联断裂、
# Web 状态异常等）。本脚本只停 G2 单元、恢复 G1 单元并复验 ONLINE；
# 不触碰正式凭证、不触碰旧 firebot-bridge、不触碰 R002。
#
# 用法（root 或 sudo）：bash scripts/rollback_g2_to_g1_readonly.sh
set -euo pipefail

G2_UNITS=(
  firebot-vehicle-agent-v1-exec.service
  firebot-ros-software-bridge.service
)
G1_UNIT="firebot-vehicle-agent-v1-readonly.service"

echo "== G2→G1 受控回滚 =="

# 1. 停 G2 执行器与软件桥（先执行器后桥，避免残留下发）
for unit in "${G2_UNITS[@]}"; do
  if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "$unit"; then
    systemctl stop "$unit" 2>/dev/null || true
    systemctl reset-failed "$unit" 2>/dev/null || true
    echo "STOPPED  $unit"
  else
    echo "SKIP     $unit（未安装）"
  fi
done

# 2. 恢复 G1 readonly Agent（unit 与程序树全程保留）
systemctl daemon-reload
systemctl start "$G1_UNIT"
sleep 8

# 3. 复验 ONLINE（G1 日志含 broker 确认的 availability online；无 UNCONFIRMED/错误）
if ! systemctl is-active --quiet "$G1_UNIT"; then
  echo "ROLLBACK=FAIL：G1 unit 未 active"
  exit 1
fi
if journalctl -u "$G1_UNIT" -n 20 --no-pager 2>/dev/null | grep -q "graceful offline"; then
  echo "ROLLBACK=FAIL：G1 日志出现离线（未重新上线）"
  exit 1
fi
if ! journalctl -u "$G1_UNIT" -n 20 --no-pager 2>/dev/null | grep -q "availability=online published"; then
  echo "ROLLBACK=FAIL：未发现 broker 确认的 availability online"
  exit 1
fi

echo "ROLLBACK=OK：G1 readonly 已恢复（$G1_UNIT active，availability online published）"
echo "复核提示：与云端 CC 共同确认正式 Web firebot-001=ONLINE；"
echo "G2 单元保持 stopped，待批准后另行处置；凭证/CA/旧桥/R002 均未触碰。"
exit 0
