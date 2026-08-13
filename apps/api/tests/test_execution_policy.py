from types import SimpleNamespace

import pytest
from app.core.errors import PlatformError
from app.db.models import Robot
from app.modules.commands.service import assert_robot_can_execute


class FakeSession:
    def __init__(self, active_task=None) -> None:
        self.active_task = active_task

    def scalar(self, _query):
        return self.active_task

    def get(self, _model, _identity):
        return None


class FakeRedis:
    def exists(self, _key):
        return False


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.modules.commands.service.get_redis", lambda: FakeRedis())


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
    with pytest.raises(PlatformError) as exc:
        assert_robot_can_execute(FakeSession(), robot(online_state="OFFLINE"), action)
    assert exc.value.status_code == 409
    assert exc.value.code == "ROBOT_OFFLINE"


def test_offline_robot_allows_estop_attempt() -> None:
    assert_robot_can_execute(FakeSession(), robot(online_state="OFFLINE"), "emergency_stop")


def test_estop_latch_blocks_normal_motion() -> None:
    with pytest.raises(PlatformError) as exc:
        assert_robot_can_execute(FakeSession(), robot(estop_active=True), "patrol")
    assert exc.value.code == "ROBOT_ESTOP_ACTIVE"


def test_active_autonomous_task_blocks_manual() -> None:
    active = SimpleNamespace(id="task-1", type="PATROL")
    with pytest.raises(PlatformError) as exc:
        assert_robot_can_execute(FakeSession(active), robot(), "manual_control")
    assert exc.value.code == "ACTIVE_TASK_CONFLICT"


def test_active_extinguish_blocks_return_dock() -> None:
    active = SimpleNamespace(id="task-1", type="EXTINGUISH")
    with pytest.raises(PlatformError) as exc:
        assert_robot_can_execute(FakeSession(active), robot(), "return_dock")
    assert exc.value.code == "ACTIVE_TASK_CONFLICT"
