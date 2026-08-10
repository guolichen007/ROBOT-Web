from types import SimpleNamespace

import pytest
from app.db.models import Robot
from app.modules.commands.service import assert_robot_can_execute
from fastapi import HTTPException


class FakeSession:
    def __init__(self, active_task=None) -> None:
        self.active_task = active_task

    def scalar(self, _query):
        return self.active_task

    def get(self, _model, _identity):
        return None


def robot(**overrides) -> Robot:
    values = {
        "id": "robot-id",
        "vehicle_id": "R001",
        "site_id": "site-id",
        "name": "R001",
        "enabled": True,
        "online_state": "ONLINE",
        "estop_active": False,
    }
    values.update(overrides)
    return Robot(**values)


@pytest.mark.parametrize("action", ["manual_control", "patrol", "extinguish", "return_dock"])
def test_offline_robot_rejects_motion_actions(action: str) -> None:
    with pytest.raises(HTTPException) as exc:
        assert_robot_can_execute(FakeSession(), robot(online_state="OFFLINE"), action)
    assert exc.value.status_code == 409


def test_offline_robot_allows_estop_attempt() -> None:
    assert_robot_can_execute(FakeSession(), robot(online_state="OFFLINE"), "emergency_stop")


def test_estop_latch_blocks_normal_motion() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_robot_can_execute(FakeSession(), robot(estop_active=True), "patrol")
    assert "急停" in str(exc.value.detail)


def test_active_autonomous_task_blocks_manual() -> None:
    active = SimpleNamespace(id="task-1", type="PATROL")
    with pytest.raises(HTTPException) as exc:
        assert_robot_can_execute(FakeSession(active), robot(), "manual_control")
    assert "显式取消" in str(exc.value.detail)


def test_active_extinguish_blocks_return_dock() -> None:
    active = SimpleNamespace(id="task-1", type="EXTINGUISH")
    with pytest.raises(HTTPException) as exc:
        assert_robot_can_execute(FakeSession(active), robot(), "return_dock")
    assert "不能回充" in str(exc.value.detail)
