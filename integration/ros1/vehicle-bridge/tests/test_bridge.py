#!/usr/bin/env python3
"""车端 Bridge 核心逻辑单元测试（无 ROS/MQTT 依赖）。

运行：cd ros-bridge && python3 tests/test_bridge.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from firebot_bridge.protocol import Protocol, TASK_CMDS, validate_command  # noqa: E402
from firebot_bridge.state import BridgeState  # noqa: E402
from firebot_bridge.downlink.command_dedup import CommandDedup  # noqa: E402
from firebot_bridge.downlink.command_validator import validate_received_command  # noqa: E402
from firebot_bridge.uplink import location as location_uplink  # noqa: E402
from firebot_bridge.uplink import sensor as sensor_uplink  # noqa: E402
from firebot_bridge.uplink import status as status_uplink  # noqa: E402
from firebot_bridge.ros.interfaces import MQTT_CMD_TO_ROS  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def sample_command(**over):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cmd = {
        "schema_version": "1.3", "message_id": str(uuid.uuid4()), "type": "command",
        "vehicle_id": "firebot-vehicle-01", "target_boot_id": "boot-boot-boot",
        "command_id": "C-test-0001", "correlation_id": str(uuid.uuid4()),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "ttl_ms": 30000, "priority": 50, "source": "WEB", "operator_id": "t",
        "cmd": "patrol", "params": {}, "task_id": str(uuid.uuid4()),
    }
    cmd.update(over)
    return cmd


class _Cfg:
    bridge_stub_mode = False
    supported_commands = ["patrol"]
    feedback_timeout_seconds = 3.0
    stub_simulate_feedback = False
    stub_feedback_simulation = "rejected"


class _LocCfg:
    def __init__(self, enabled: bool) -> None:
        self.location_enabled = enabled
        self.site_code = "P1"
        self.map_code = "mymap"
        self.map_version = "1"
        self.map_checksum = "d74d60b525f20e08a69857e01cd4fa01d58710b48c61ced1fd7e07737312fe6d"


def main() -> int:
    print("=== validate_command ===")
    ok, reason = validate_command(sample_command(), "boot-boot-boot")
    check("合法 patrol 通过", ok and reason is None)
    ok, reason = validate_command(sample_command(cmd="patrol"), "other-boot")
    check("boot 不匹配拒绝 ROBOT_BOOT_SESSION_UNKNOWN", not ok and reason == "ROBOT_BOOT_SESSION_UNKNOWN")
    ok, reason = validate_command(sample_command(cmd="emergency_stop", target_boot_id=None), "other-boot")
    check("emergency_stop target_boot_id=None 放行", ok)
    ok, reason = validate_command(
        sample_command(cmd="emergency_stop", target_boot_id="other-boot"), "boot-boot-boot"
    )
    check("emergency_stop 显式异 boot 拒绝", not ok and reason == "ROBOT_BOOT_SESSION_UNKNOWN")
    # 过期
    exp = sample_command()
    exp["issued_at"] = "2026-08-24T00:00:00+00:00"
    exp["expires_at"] = "2026-08-24T00:00:01+00:00"
    ok, reason = validate_command(exp, "boot-boot-boot")
    check("过期拒绝 COMMAND_EXPIRED", not ok and reason == "COMMAND_EXPIRED")
    ok, reason = validate_command(sample_command(cmd="START"), "boot-boot-boot")
    check("非枚举 cmd 拒绝", not ok)
    ok, reason = validate_command(sample_command(schema_version="9.9"), "boot-boot-boot")
    check("schema_version 不支持拒绝", not ok)

    print("=== command_validator（能力/任务锁前置）===")
    ok, reason = validate_received_command(sample_command(), "boot-boot-boot", _Cfg())
    check("生产: patrol 在 supported 中通过", ok)
    stub = _Cfg()
    stub.bridge_stub_mode = True
    ok, _ = validate_received_command(sample_command(cmd="extinguish"), "boot-boot-boot", stub)
    check("stub: 未声明命令也放行（联调）", ok)
    ok, reason = validate_received_command(sample_command(task_id=None), "boot-boot-boot", _Cfg())
    check("patrol 缺 task_id 拒绝", not ok and reason == "INVALID_PROTOCOL_MESSAGE")
    check("TASK_CMDS 含 patrol/return_dock/extinguish",
          TASK_CMDS == {"patrol", "return_dock", "extinguish"})

    print("=== command_dedup 幂等重放 ===")
    state = BridgeState()
    dd = CommandDedup(state)
    check("首次未见", dd.lookup("C-1") is None)
    dd.register("C-1", {"command_id": "C-1", "cmd": "patrol"})
    rec = dd.lookup("C-1")
    check("注册后有记录且 ACK 为空", rec is not None and rec.get("ack") is None)
    dd.remember_ack("C-1", {"command_id": "C-1", "status": "accepted"})
    dd.remember_task_status("C-1", {"task_id": "t1", "status": "executing"})
    rec = dd.lookup("C-1")
    check("重放 ACK 存在", rec["ack"]["status"] == "accepted")
    check("重放 task_status 存在", rec["task_status"]["status"] == "executing")
    dd.mark_terminal("C-1")
    check("终态标记", dd.lookup("C-1")["terminal"] is True)

    print("=== status partial（只发真实字段）===")
    proto = Protocol("firebot-vehicle-01", str(uuid.uuid4()))
    s = BridgeState()
    s.set_battery(82.4)
    msg = status_uplink.make_status(proto, s)
    check("battery 存在", msg is not None and msg.get("battery") == 82.4)
    check("无伪造 mode（None 不发）", msg is not None and "mode" not in msg)
    check("无伪造 estop", msg is not None and "estop_active" not in msg)
    s2 = BridgeState()
    check("无任何真实字段不发 status", status_uplink.make_status(proto, s2) is None)

    print("=== sensor smoke-only（capability-driven）===")
    s3 = BridgeState()
    check("无 smoke 不发布 sensor", sensor_uplink.make_sensor(proto, s3) is None)
    s3.set_smoke(18.2)
    msg = sensor_uplink.make_sensor(proto, s3)
    check("smoke 存在", msg is not None and msg.get("smoke") == 18.2)
    check("无 bottom_ir 不出现", msg is not None and "bottom_ir" not in msg)
    check("无 top_ir_max 不出现", msg is not None and "top_ir_max" not in msg)

    print("=== 任务锁 release 不伪造 IDLE ===")
    s4 = BridgeState()
    s4.acquire_task("t-1")
    s4.release_task()
    check("release 后 active_task_id 为空", s4.active_task_id is None)
    check("release 不伪造 mode", s4.mode is None)

    print("=== location 默认门控 ===")
    s5 = BridgeState()
    s5.set_location({"position": {"x": 1.2, "y": 1.2, "theta": 0.0}})
    check("默认 location_enabled=false 不发", location_uplink.make_location(proto, s5, _LocCfg(False)) is None)
    msg = location_uplink.make_location(proto, s5, _LocCfg(True))
    check("location_enabled=true 发布", msg is not None and msg["position"]["x"] == 1.2)

    print("=== ROS command 映射 ===")
    check("patrol→PATROL_START", MQTT_CMD_TO_ROS["patrol"] == "PATROL_START")
    check("emergency_stop→EMERGENCY_STOP", MQTT_CMD_TO_ROS["emergency_stop"] == "EMERGENCY_STOP")
    check("8 命令齐全", len(MQTT_CMD_TO_ROS) == 8)

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
