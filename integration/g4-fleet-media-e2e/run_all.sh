#!/bin/bash
# G4-Fleet-R2 标准化接入验收：四阶段各自独立进程运行（单阶段 ≤ 175s）。
# 前置：隔离 PG/Redis（firebot_e2e）、mosquitto、MediaMTX、Ubuntu 20.04 用户态 focalpy.sh、libsrt 1.4.4 产物。
# 用法：bash integration/g4-fleet-media-e2e/run_all.sh   → 每阶段末行 ACCEPTANCE_<PHASE>=PASS|FAIL
set -u
cd "$(dirname "$0")/../.."
rc=0
for phase in new adopt001 negatives outage; do
  G4_OUTAGE_S="${G4_OUTAGE_S:-60}" timeout 175 python integration/g4-fleet-media-e2e/run_acceptance.py "$phase" \
    > "/tmp/g4-acceptance-$phase.txt" 2>&1 || rc=1
  grep -E "^(ACCEPTANCE|FAIL)" "/tmp/g4-acceptance-$phase.txt"
done
exit $rc
