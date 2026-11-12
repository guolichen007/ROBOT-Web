"""G2-R2 P1：ROS 软件桥停止关联收敛（确定性：伪 rospy，真实桥代码）。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from services.vehicle_agent_v1_exec.ros.software_bridge import RosSoftwareBridge


class _Pub:
    def __init__(self, *a, **k):
        self.sent = []

    def get_num_connections(self):
        return 1

    def publish(self, message):
        self.sent.append(message)


class _Sub:
    def __init__(self, topic, kind, callback, queue_size=10):
        self.callback = callback

    def get_num_connections(self):
        return 1


class _Msg(SimpleNamespace):
    def __init__(self, **kw):
        super().__init__(**{"command_id": "", "action": "", "params": "", **kw})


def make_bridge():
    fake = SimpleNamespace(Publisher=_Pub, Subscriber=_Sub)
    return RosSoftwareBridge(fake, (_Msg, _Msg, _Msg), "/firebot_001/software")


def fb(command_id, action, status, detail="", software_test=True):
    return _Msg(
        command_id=command_id,
        action=action,
        status=status,
        detail=detail,
        software_test=software_test,
        ts="2026-11-12T00:00:00+00:00",
    )


def run(bridge, command_id, action, out):
    def target():
        bridge.run_command({"command_id": command_id, "command": action, "params": {}}, out.append)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def wait(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def start_patrol(bridge, pid="P1"):
    out: list[dict] = []
    thread = run(bridge, pid, "patrol", out)
    assert wait(lambda: pid in bridge.pending)
    bridge._on_feedback(fb(pid, "patrol", "ACCEPTED"))
    assert wait(lambda: out)
    return out, thread


def test_normal_stop_correlates_only_active_patrol():
    bridge = make_bridge()
    patrol_out, patrol_thread = start_patrol(bridge)
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    assert bridge.pending["S1"].target == "P1"
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    bridge._on_feedback(fb("P1", "patrol", "STOPPED", "stopped at round 2"))
    stop_thread.join(3), patrol_thread.join(3)
    assert patrol_out[-1]["state"] == "STOPPED"  # patrol 独立终态
    assert stop_out[-1]["state"] == "COMPLETED" and stop_out[-1]["ros_status"] == "STOPPED"
    assert stop_out[-1]["command_id"] == "S1" and stop_out[-1]["stopped_command_id"] == "P1"


def test_untrusted_stopped_never_completes_stop():
    bridge = make_bridge()
    patrol_out, patrol_thread = start_patrol(bridge)
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    bridge._on_feedback(fb("P1", "patrol", "STOPPED", software_test=False))
    patrol_thread.join(3)
    assert patrol_out[-1]["state"] == "FAILED"
    assert patrol_out[-1]["reason_code"] == "SOFTWARE_TEST_FLAG_MISSING"
    assert not [o for o in stop_out if o["state"] == "COMPLETED"]
    bridge.pending.pop("S1").inbox.put({"state": "FAILED"})  # 结束线程
    stop_thread.join(3)


def test_stopped_before_stop_accepted_is_not_success():
    bridge = make_bridge()
    patrol_out, patrol_thread = start_patrol(bridge)
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    bridge._on_feedback(fb("P1", "patrol", "STOPPED"))  # 先于 stop ACCEPTED
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    stop_thread.join(3), patrol_thread.join(3)
    assert stop_out[-1]["state"] == "REJECTED"
    assert stop_out[-1]["reason_code"] == "STOP_NOT_EFFECTIVE"


def test_natural_completion_race_is_not_success():
    bridge = make_bridge()
    patrol_out, patrol_thread = start_patrol(bridge)
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    bridge._on_feedback(fb("P1", "patrol", "COMPLETED", "finished 3 rounds"))
    stop_thread.join(3), patrol_thread.join(3)
    assert patrol_out[-1]["state"] == "COMPLETED"
    assert stop_out[-1]["state"] == "REJECTED"
    assert stop_out[-1]["reason_code"] == "PATROL_ALREADY_FINISHED"


def test_foreign_msg_id_and_action_mismatch_not_chained():
    bridge = make_bridge()
    patrol_out, patrol_thread = start_patrol(bridge)
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    # 其他车辆 / 其他 msg_id 的 STOPPED：不得串联到本 stop
    bridge._on_feedback(fb("OTHER-002", "patrol", "STOPPED"))
    time.sleep(0.2)
    assert not [o for o in stop_out if o["state"] in {"COMPLETED", "REJECTED", "FAILED"}]
    # 相同 command_id 但 action 不符：判定 FAILED，不触发停止关联
    bridge._on_feedback(fb("P1", "stop_patrol", "STOPPED"))
    patrol_thread.join(3)
    assert patrol_out[-1]["reason_code"] == "FEEDBACK_ACTION_MISMATCH"
    assert not [o for o in stop_out if o["state"] == "COMPLETED"]
    bridge.pending.pop("S1").inbox.put({"state": "FAILED"})
    stop_thread.join(3)


def test_stop_without_tracked_patrol_is_unconfirmed():
    bridge = make_bridge()
    stop_out: list[dict] = []
    stop_thread = run(bridge, "S1", "stop_patrol", stop_out)
    assert wait(lambda: "S1" in bridge.pending)
    assert bridge.pending["S1"].target is None
    bridge._on_feedback(fb("S1", "stop_patrol", "ACCEPTED"))
    stop_thread.join(3)
    assert stop_out[-1]["state"] == "FAILED"
    assert stop_out[-1]["reason_code"] == "STOP_TARGET_UNTRACKED"
