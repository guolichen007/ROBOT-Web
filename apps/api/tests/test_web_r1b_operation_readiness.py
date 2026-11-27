"""WEB-R1B OperationReadiness: server authority, contract-valid, mirrors the final gate."""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password
from app.db.models import Robot, User
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

from services import contracts_v21 as c


@pytest.fixture(autouse=True)
def baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.password_hash = hash_password(get_settings().effective_admin_password)
        admin.must_change_password = False
        admin.status = "ACTIVE"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        r = value.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": get_settings().effective_admin_password},
        )
        value.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        yield value


def _ready(client, vid="R002") -> dict:
    r = client.get(f"/api/v1/robots/{vid}/operation-readiness")
    assert r.status_code == 200, r.text
    c.validate("OperationReadiness", r.json())  # contract draft.3 valid
    return r.json()


def test_real_default_is_frozen_all_including_software_estop(client, monkeypatch) -> None:
    s = get_settings()
    for k, v in {
        "downlink_frozen": True,
        "firebot_deployment_env": "REAL",
        "canonical_downlink_allowlist": "",
    }.items():
        monkeypatch.setattr(s, k, v)
    r = _ready(client)
    assert r["source"] == "SERVER_OPERATION_READINESS" and r["environment"] == "REAL"
    assert r["control_frozen"] is True and r["freeze_scope"] == "ALL"
    assert "REAL_CONTROL_NOT_AUTHORIZED" in r["freeze_reasons"]
    assert not any(a["allowed"] for a in r["actions"].values())
    assert "CONTROL_FROZEN" in r["actions"]["emergency_stop"]["reasons"]


def test_protective_stop_never_allowed_until_implemented(client, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "downlink_frozen", False)
    monkeypatch.setattr(s, "canonical_downlink_allowlist", "R002")
    r = _ready(client)
    assert r["actions"]["protective_stop"]["allowed"] is False
    assert "PROTECTIVE_STOP_NOT_IMPLEMENTED" in r["actions"]["protective_stop"]["reasons"]


def test_online_is_not_ready_offline_robot_denies_everything(client, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "downlink_frozen", False)
    monkeypatch.setattr(s, "canonical_downlink_allowlist", "R002")
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        previous, robot.online_state = robot.online_state, "OFFLINE"
    try:
        r = _ready(client)
        assert not any(a["allowed"] for a in r["actions"].values())
        assert "ROBOT_OFFLINE" in r["actions"]["stop_motion"]["reasons"]
    finally:
        with SessionLocal.begin() as db:
            db.scalar(select(Robot).where(Robot.vehicle_id == "R002")).online_state = previous


def test_snapshot_carries_same_authority_and_deployment_env(client) -> None:
    snap = client.get("/api/v1/monitor/snapshot").json()
    robot = next(r for r in snap["robots"] if r["vehicle_id"] == "R002")
    c.validate("OperationReadiness", robot["operation_readiness"])
    assert robot["deployment_environment"] in {"REAL", "SIM"}


def test_v1_vehicle_keeps_independent_v1_freeze(client, monkeypatch) -> None:
    from app.modules.commands.operation_readiness import freeze_state

    monkeypatch.setattr(get_settings(), "vehicle_agent_v1_downlink_enabled", False)
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        robot.agent_protocol = "VEHICLE_AGENT_V1"  # in-memory only, not committed
        frozen, reasons, scope = freeze_state(robot, get_settings())
        db.rollback()
    assert frozen and "V1_DOWNLINK_DISABLED" in reasons and scope == "ALL"
    assert "NOT_ALLOWLISTED" not in reasons  # 001 never depends on the Canonical allowlist
