from __future__ import annotations

import json

import pytest
from app.core.errors import PlatformError
from app.db.models import Robot, RobotIntegrationProfile, Task
from app.modules.robots.router import assert_robot_can_disable
from app.modules.system.router import assemble_robot_state, build_operation_context


def make_robot(
    vehicle_id: str,
    robot_id: str,
    *,
    enabled: bool = True,
    online: str = "ONLINE",
    mode: str = "IDLE",
    estop: bool = False,
) -> Robot:
    return Robot(
        id=robot_id,
        vehicle_id=vehicle_id,
        site_id="site",
        name=vehicle_id,
        model="MODEL",
        enabled=enabled,
        online_state=online,
        current_mode=mode,
        estop_active=estop,
    )


def make_task(
    task_id: str,
    robot_id: str,
    *,
    task_type: str = "PATROL",
    status: str = "CANCELLED",
    parameters: dict | None = None,
) -> Task:
    return Task(
        id=task_id,
        task_code=f"T-{task_id}",
        robot_id=robot_id,
        type=task_type,
        status=status,
        phase="CANCELLED",
        progress=40.0,
        map_id_snapshot="map",
        map_version_snapshot="1",
        semantic_revision_snapshot=1,
        parameters_json=parameters or {},
        created_by="admin",
    )


class FakeDb:
    def __init__(self, scalar_results: list | None = None):
        self._scalars = list(scalar_results or [])
        self.added: list = []
        self.committed = False

    def scalar(self, stmt):
        return self._scalars.pop(0) if self._scalars else None

    def get(self, model, key):
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True


class FakeRedis:
    def __init__(self, lease: dict | None = None):
        self._lease = lease

    def get(self, key: str):
        if self._lease is None:
            return None
        return json.dumps(self._lease)


# ---- section 38: operation context is per-vehicle ----
def test_operation_context_is_per_vehicle() -> None:
    r001 = make_robot("R001", "robot-a")
    real = make_robot("firebot-vehicle-01", "robot-b")
    cancelled_patrol = make_task(
        "t1",
        "robot-a",
        parameters={"resume_state": "AVAILABLE", "live_route_cursor": {"target_waypoint_index": 5}},
    )
    # build_operation_context issues 3 scalar queries: active, cancelled PATROL,
    # cancelled RETURN_DOCK.
    ctx_r001 = build_operation_context(FakeDb([None, cancelled_patrol, None]), r001)
    ctx_real = build_operation_context(FakeDb([None, None, None]), real)
    assert ctx_r001["state"] == "PAUSED"
    assert ctx_r001["kind"] == "PATROL"
    assert ctx_r001["can_continue"] is True
    assert ctx_real["state"] == "IDLE"
    assert ctx_real["kind"] is None


# ---- section 39: enabled is a stable snapshot field ----
def test_enabled_stable_when_redis_latest_present() -> None:
    robot = make_robot("firebot-vehicle-01", "robot-b", enabled=False)
    raw = json.dumps(
        {"vehicle_id": "firebot-vehicle-01", "x": 1.2, "y": 1.2, "mode": "IDLE", "battery": 86}
    )
    state = assemble_robot_state(robot, raw)
    assert state["enabled"] is False
    assert state["vehicle_id"] == "firebot-vehicle-01"
    assert state["battery"] == 86  # realtime wins when present
    assert state["current_map_version"] == robot.current_map_version


def test_assemble_robot_state_without_redis_falls_back_to_db() -> None:
    robot = make_robot("R001", "robot-a", enabled=True, mode="IDLE")
    state = assemble_robot_state(robot, None)
    assert state["enabled"] is True
    assert state["mode"] == "IDLE"
    assert state["name"] == "R001"


# ---- section 40: disable safety gates ----
def test_disable_idle_robot_passes() -> None:
    robot = make_robot("R001", "robot-a")
    assert_robot_can_disable(FakeDb([None]), robot, FakeRedis())


def test_disable_blocked_by_active_task() -> None:
    robot = make_robot("R001", "robot-a")
    executing = make_task("t-exec", "robot-a", status="EXECUTING")
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_disable(FakeDb([executing]), robot, FakeRedis())
    assert raised.value.code == "ROBOT_DISABLE_ACTIVE_TASK"


def test_disable_blocked_by_active_return_dock() -> None:
    robot = make_robot("R001", "robot-a")
    returning = make_task("t-ret", "robot-a", task_type="RETURN_DOCK", status="EXECUTING")
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_disable(FakeDb([returning]), robot, FakeRedis())
    assert raised.value.code == "ROBOT_DISABLE_ACTIVE_TASK"


def test_disable_blocked_by_estop() -> None:
    robot = make_robot("R001", "robot-a", estop=True)
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_disable(FakeDb([None]), robot, FakeRedis())
    assert raised.value.code == "ROBOT_DISABLE_ESTOP_ACTIVE"


def test_disable_blocked_by_non_idle_mode() -> None:
    robot = make_robot("R001", "robot-a", mode="MANUAL")
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_disable(FakeDb([None]), robot, FakeRedis())
    assert raised.value.code == "ROBOT_DISABLE_NOT_IDLE"


def test_disable_blocked_by_manual_lease() -> None:
    robot = make_robot("R001", "robot-a")
    lease = {"lease_id": "l1", "user_id": "u1", "robot_id": "robot-a", "vehicle_id": "R001"}
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_disable(FakeDb([None]), robot, FakeRedis(lease))
    assert raised.value.code == "ROBOT_DISABLE_MANUAL_LEASE"


def test_offline_non_idle_robot_can_still_disable() -> None:
    # The mode guard only applies while the robot is online.
    robot = make_robot("R001", "robot-a", online="OFFLINE", mode="PATROL")
    assert_robot_can_disable(FakeDb([None]), robot, FakeRedis())


# ---- section 40 G/H: enable endpoint never auto-promotes control readiness ----
def test_enable_endpoint_does_not_promote_readiness(monkeypatch) -> None:
    import app.modules.robots.router as rr
    from app.modules.robots.router import EnabledRequest, set_platform_enabled

    robot = make_robot("R001", "robot-a", enabled=False)
    profile = RobotIntegrationProfile(
        robot_id="robot-a",
        source_kind="MOCK",
        control_contract_verified=False,
        ack_contract_verified=False,
        map_contract_verified=False,
    )

    class Db(FakeDb):
        def get(self, model, key):
            return profile if model is RobotIntegrationProfile else None

    db = Db()
    audit: dict = {}
    monkeypatch.setattr(rr, "find_robot", lambda db, rid: robot)
    monkeypatch.setattr(rr, "request_meta", lambda request: {})
    monkeypatch.setattr(rr, "write_audit", lambda db, **kw: audit.update(kw))
    monkeypatch.setattr(rr, "append_event", lambda event_type, payload: None)
    monkeypatch.setattr(rr, "get_redis", lambda: FakeRedis())

    auth = type("Auth", (), {"user": type("User", (), {"id": "u1"})()})()
    set_platform_enabled("R001", EnabledRequest(enabled=True), None, db, auth)

    assert robot.enabled is True
    assert audit["action"] == "ROBOT_PLATFORM_ENABLED"
    assert audit["before"] == {"enabled": False}
    assert audit["after"] == {"enabled": True}
    assert profile.control_contract_verified is False
    assert profile.ack_contract_verified is False
    assert profile.map_contract_verified is False


def test_disable_endpoint_audit_records_real_before_value(monkeypatch) -> None:
    import app.modules.robots.router as rr
    from app.modules.robots.router import EnabledRequest, set_platform_enabled

    # An already-disabled robot: the audit "before" must be the real stored
    # value (False), never a derived `not payload.enabled`.
    robot = make_robot("R001", "robot-a", enabled=False)

    class Db(FakeDb):
        def get(self, model, key):
            return None

    db = Db()
    audit: dict = {}
    monkeypatch.setattr(rr, "find_robot", lambda db, rid: robot)
    monkeypatch.setattr(rr, "request_meta", lambda request: {})
    monkeypatch.setattr(rr, "write_audit", lambda db, **kw: audit.update(kw))
    monkeypatch.setattr(rr, "append_event", lambda event_type, payload: None)
    monkeypatch.setattr(rr, "get_redis", lambda: FakeRedis())

    auth = type("Auth", (), {"user": type("User", (), {"id": "u1"})()})()
    set_platform_enabled("R001", EnabledRequest(enabled=False), None, db, auth)

    assert audit["before"] == {"enabled": False}
    assert audit["after"] == {"enabled": False}
