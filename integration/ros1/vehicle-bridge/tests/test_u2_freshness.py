"""U2 freshness 单元测试（pytest 兼容 + 可直接 python3 运行）。

不依赖 pytest：test_* 函数会被 pytest 收集；直接运行则在 __main__ 里手动调用全部。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firebot_bridge.state import BridgeState
from tools.field_console import _ZH_PATH, _ZH_LABEL


def test_battery_new_fresh():
    s = BridgeState()
    assert s.set_battery(61.7) is False  # 首次不是 recovered
    assert s.last_battery == 61.7
    assert s.battery_updated_monotonic is not None


def test_battery_same_value_keeps_fresh():
    s = BridgeState()
    s.set_battery(61.7)
    t1 = s.battery_updated_monotonic
    time.sleep(0.01)
    s.set_battery(61.7)  # 同值也必须刷新时间戳
    assert s.battery_updated_monotonic > t1


def test_battery_stale_after_ttl():
    s = BridgeState()
    s.set_battery(61.7)
    r = s.expire_stale_telemetry(5, 5, now=s.battery_updated_monotonic + 6)
    assert r["battery_stale"] is True
    assert s.last_battery is None


def test_stale_triggered_once():
    s = BridgeState()
    s.set_battery(61.7)
    r1 = s.expire_stale_telemetry(5, 5, now=s.battery_updated_monotonic + 6)
    assert r1["battery_stale"] is True
    r2 = s.expire_stale_telemetry(5, 5, now=s.battery_updated_monotonic + 100)
    assert r2["battery_stale"] is False  # 已清除，不再触发


def test_battery_recovered_once():
    s = BridgeState()
    s.set_battery(61.7)
    s.expire_stale_telemetry(5, 5, now=s.battery_updated_monotonic + 6)  # stale
    assert s.set_battery(62.3) is True  # recovered
    assert s.set_battery(62.3) is False  # 第二次不是 recovered
    assert s.last_battery == 62.3


def test_smoke_same_behavior():
    s = BridgeState()
    assert s.set_smoke(0.123) is False
    assert s.last_smoke == 0.123
    t1 = s.smoke_updated_monotonic
    s.set_smoke(0.123)
    assert s.smoke_updated_monotonic >= t1
    r = s.expire_stale_telemetry(5, 5, now=s.smoke_updated_monotonic + 6)
    assert r["smoke_stale"] is True
    assert s.last_smoke is None
    assert s.set_smoke(0.234) is True  # recovered
    assert s.set_smoke(0.234) is False


def test_clear_ros_telemetry():
    s = BridgeState()
    s.set_battery(61.7)
    s.set_smoke(0.123)
    s.acquire_task("T-1")
    s.apply_status({"active_task_id": "T-2"})
    s.clear_ros_telemetry()
    assert s.last_battery is None
    assert s.last_smoke is None
    assert s.battery_updated_monotonic is None
    assert s.smoke_updated_monotonic is None
    assert s.task_lock_id == "T-1"  # 绝不清 task_lock_id
    assert s.reported_active_task_id is None


def test_task_semantics_no_regression():
    s = BridgeState()
    assert s.acquire_task("") is False  # 空 task_id 不能形成锁
    assert s.acquire_task("T-1") is True
    assert s.acquire_task("T-2") is False  # 已有锁
    s.release_task()
    assert s.task_lock_id is None
    s.apply_status({"active_task_id": "T-3"})
    assert s.reported_active_task_id == "T-3"
    assert s.snapshot_telemetry()["active_task_id"] == "T-3"


def test_field_console_zh_stale_recovered():
    assert _ZH_PATH["ros.battery.stale"] == "电量数据源"
    assert _ZH_PATH["ros.battery.recovered"] == "电量数据源"
    assert _ZH_PATH["ros.smoke.stale"] == "烟雾数据源"
    assert _ZH_LABEL["ros.battery.stale"] == "已超时"
    assert _ZH_LABEL["ros.battery.recovered"] == "已恢复"
    assert _ZH_LABEL["ros.smoke.stale"] == "已超时"
    assert _ZH_LABEL["ros.smoke.recovered"] == "已恢复"


def test_feedback_cmd_message_no_regression():
    from firebot_bridge.field_trace import _EVENT_ALLOWED_KEYS
    # 主机修复回归：ros.feedback.rx 白名单含 cmd + message
    assert "cmd" in _EVENT_ALLOWED_KEYS["ros.feedback.rx"]
    assert "message" in _EVENT_ALLOWED_KEYS["ros.feedback.rx"]
    # U2 新增：ros.smoke.rx 白名单含 source；stale/recovered 白名单含 source
    assert "source" in _EVENT_ALLOWED_KEYS["ros.smoke.rx"]
    assert "source" in _EVENT_ALLOWED_KEYS["ros.battery.stale"]
    assert "source" in _EVENT_ALLOWED_KEYS["ros.smoke.recovered"]


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} 通过")
    sys.exit(0 if passed == len(tests) else 1)
