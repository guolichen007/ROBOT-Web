#!/usr/bin/env python3
"""Fleet 身份隔离测试：MQTT topic namespace 必须由 DEVICE_ID 派生且两两不相交。

对应企业验收：vehicle-01 credential 不能 publish/subscribe vehicle-02 namespace。
本测试验证 namespace 派生合同（真实 Mosquitto ACL 由 fleet-register 的
username=DEVICE_ID + `robot/%u/#` pattern 保证，需真机/容器二次验证）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firebot_bridge.protocol import Protocol

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def topic_ns(device_id: str) -> str:
    return f"robot/{device_id}/"


def main() -> int:
    a = Protocol("firebot-vehicle-01", "boot-a")
    b = Protocol("firebot-vehicle-02", "boot-b")

    # 1) topic namespace 由 DEVICE_ID 派生
    check(
        "01 command topic = robot/firebot-vehicle-01/command",
        a.topic("command") == "robot/firebot-vehicle-01/command",
    )
    check(
        "02 command topic = robot/firebot-vehicle-02/command",
        b.topic("command") == "robot/firebot-vehicle-02/command",
    )

    # 2) 两设备 namespace 不相交
    a_cmd = a.topic("command")
    b_cmd = b.topic("command")
    check("01 command ≠ 02 command", a_cmd != b_cmd)

    # 3) 跨设备 namespace 判定（ACL 授权契约）：A 的设备 namespace 不含 B 的 topic
    ns_a = topic_ns("firebot-vehicle-01")
    check("01 namespace 不含 02 command", not a_cmd.replace(ns_a, "").startswith("firebot-vehicle-02"))
    check("02 command 不属于 01 namespace", not b_cmd.startswith(ns_a))
    check(
        "01 namespace 只含 01 前缀",
        a_cmd.startswith(ns_a) and "firebot-vehicle-02" not in a_cmd,
    )

    # 4) 所有 topic 都在本设备 namespace 下
    for t in ("command", "status", "location", "heartbeat"):
        assert a.topic(t).startswith(ns_a), a.topic(t)
        assert b.topic(t).startswith(topic_ns("firebot-vehicle-02")), b.topic(t)
    check("全部 topic 都在本设备 namespace 下", True)

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
