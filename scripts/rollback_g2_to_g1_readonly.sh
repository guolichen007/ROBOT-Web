#!/usr/bin/env bash
# G2 → G1 readonly 受控回滚（001 车端，G2-R2 加固版）。
#
# 加固要求（本版已实现）：
#  1) systemctl stop 失败绝不报告 STOPPED：停止后逐单元验证 is-active=inactive；
#  2) 恢复 G1 只核验「本次启动之后」的证据：启动前记录时间戳，启动后读取本次
#     InvocationID，journal 按 --since 过滤，历史上线记录不参与判定；
#  3) 云端状态不由本脚本断言：脚本最多输出 ROLLBACK_LOCAL_PASS + CLOUD_PENDING，
#     由云端 CC 确认新鲜 last_seen 与正式 API ONLINE 后才可人工标注 ROLLBACK_VERIFIED；
#  4) 旧 firebot-bridge 与 R002 全程不触碰。
#
# 用法（root 或 sudo）：bash scripts/rollback_g2_to_g1_readonly.sh
set -euo pipefail

G2_UNITS=(
  firebot-vehicle-agent-v1-exec.service
  firebot-ros-software-bridge.service
)
G1_UNIT="firebot-vehicle-agent-v1-readonly.service"
WAIT_ONLINE_SECONDS="${ROLLBACK_WAIT_ONLINE:-30}"

echo "== G2→G1 受控回滚（加固版） =="

# ---- 1. 停 G2 执行器与软件桥，并实际核验 inactive ----
for unit in "${G2_UNITS[@]}"; do
  if ! systemctl list-unit-files "$unit" 2>/dev/null | grep -q "$unit"; then
    echo "SKIP     $unit（未安装，无需停止）"
    continue
  fi
  if systemctl is-active --quiet "$unit"; then
    systemctl stop "$unit"
    sleep 2
  fi
  if systemctl is-active --quiet "$unit"; then
    echo "ROLLBACK=FAIL：$unit 停止失败（is-active 仍为 active）"
    exit 1
  fi
  systemctl reset-failed "$unit" 2>/dev/null || true
  echo "STOPPED  $unit（已验证 inactive）"
done

# ---- 2. 记录本次恢复的时间与 invocation，仅核验本次启动后的证据 ----
START_TS="$(date --iso-8601=seconds)"
echo "G1_START_TS=$START_TS"
systemctl daemon-reload
systemctl start "$G1_UNIT"
sleep 3
INVOCATION_ID="$(systemctl show "$G1_UNIT" -p InvocationID --value 2>/dev/null || echo UNKNOWN)"
echo "G1_INVOCATION_ID=$INVOCATION_ID"

# ---- 3. 等待本次启动后的 CONNACK / availability PUBACK（不超过窗口） ----
CONFIRMED=0
for _ in $(seq 1 "$WAIT_ONLINE_SECONDS"); do
  if systemctl is-active --quiet "$G1_UNIT" \
     && journalctl -u "$G1_UNIT" --since "$START_TS" --no-pager 2>/dev/null \
        | grep -q "availability=online published" \
     && ! journalctl -u "$G1_UNIT" --since "$START_TS" --no-pager 2>/dev/null \
        | grep -qE "UNCONFIRMED|connect failed|refusing to start"; then
    CONFIRMED=1
    break
  fi
  sleep 1
done
if [ "$CONFIRMED" != "1" ]; then
  echo "ROLLBACK=FAIL：本次启动窗口内未确认 availability online PUBACK"
  echo "（只统计 --since $START_TS 之后的日志；历史记录不参与判定）"
  exit 1
fi

# ---- 4. 本地侧结论 + 云端待确认（不谎报完整 VERIFIED） ----
echo "ROLLBACK_LOCAL_PASS：G1 readonly 已恢复（$G1_UNIT active）"
echo "  本次启动时间：$START_TS"
echo "  本次 InvocationID：$INVOCATION_ID"
echo "  本次启动后证据：availability=online published（broker PUBACK 确认），无错误/UNCONFIRMED"
echo "  CLOUD_PENDING：请云端 CC 核验 firebot-vehicle-01 新鲜 last_seen 与正式 API ONLINE；"
echo "  云端确认后方可在验收记录中标注 ROLLBACK_VERIFIED（本脚本不代云端断言）。"
echo "  旧 firebot-bridge 与 R002 全程未触碰。"
exit 0
