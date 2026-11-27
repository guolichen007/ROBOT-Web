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


# ---------------------------------------------------------------- WEB-R1C R1C-2
R1C_ACTIONS = {
    "patrol_pause",
    "patrol_resume",
    "navigate_to_point",
    "start_vehicle",
    "shutdown_vehicle",
    "fire_shield_open",
    "fire_shield_stop",
    "fire_shield_close",
}


def test_r1c_every_console_action_is_server_decided_and_contract_valid(client, monkeypatch) -> None:
    """The console renders pause/resume/goal/device buttons; each must have a server entry."""
    s = get_settings()
    monkeypatch.setattr(s, "downlink_frozen", False)
    monkeypatch.setattr(s, "canonical_downlink_allowlist", "R002")
    r = _ready(client)  # schema + semantic validation of the whole document
    assert R1C_ACTIONS <= set(r["actions"])
    # canonical vehicles: no pause / start / shutdown / fire-shield path exists → never allowed,
    # each with an explicit reason code (contract: reasons minItems 1 when not allowed)
    for name in ("patrol_pause", "start_vehicle", "shutdown_vehicle"):
        assert r["actions"][name]["allowed"] is False and r["actions"][name]["reasons"]
    for name in ("fire_shield_open", "fire_shield_stop", "fire_shield_close"):
        assert r["actions"][name]["allowed"] is False
        assert "DEVICE_FEEDBACK_CONTRACT_NOT_VERIFIED" in r["actions"][name]["reasons"]
        assert r["actions"][name]["confirm"] == "HOLD"
    # goal / resume share the canonical autonomous readiness — never more permissive than patrol
    for name in ("navigate_to_point", "patrol_resume"):
        assert r["actions"][name]["allowed"] <= r["actions"]["patrol_start"]["allowed"]


def test_r1c_real_freeze_blocks_every_new_action(client, monkeypatch) -> None:
    s = get_settings()
    for k, v in {
        "downlink_frozen": True,
        "firebot_deployment_env": "REAL",
        "canonical_downlink_allowlist": "",
    }.items():
        monkeypatch.setattr(s, k, v)
    r = _ready(client)
    for name in R1C_ACTIONS:
        assert r["actions"][name]["allowed"] is False
        assert "CONTROL_FROZEN" in r["actions"][name]["reasons"]


def test_r1c_v1_vehicle_maps_v1_commands_without_canonical_fallback() -> None:
    """vehicle_agent v1: start/close/patrol/... follow vehicle_agent_ready; canonical-only
    actions (stop_motion, e-stop, manual) stay denied — no cross-protocol bypass."""
    from datetime import UTC, datetime

    from app.modules.commands.operation_readiness import operation_readiness

    class R:  # minimal robot double (pure function under test)
        vehicle_id = "R001"
        agent_protocol = "VEHICLE_AGENT_V1"
        enabled = True
        online_state = "ONLINE"
        boot_id = "11111111-1111-4111-8111-111111111111"

    class S:
        firebot_deployment_env = "REAL"
        downlink_frozen = False
        sim_downlink_enabled = False
        canonical_downlink_allowlist_set: set[str] = set()

        @staticmethod
        def vehicle_downlink_allowed(_vid: str) -> bool:
            return True

    readiness = {
        "vehicle_agent_ready": {"start": True, "close": False, "patrol": True, "navigate": False},
        "safety_command_ready": {"stop_motion": False},
        "readiness_reasons": [],
    }
    perms = {"robot.control.task", "robot.control.stop", "patrol.create", "robot.read"}
    doc = operation_readiness(
        R(), readiness, settings=S(), permissions=perms, now=datetime.now(UTC)
    )
    c.validate("OperationReadiness", doc)
    a = doc["actions"]
    assert a["start_vehicle"]["allowed"] is True
    assert a["patrol_start"]["allowed"] is True
    assert a["shutdown_vehicle"]["allowed"] is False and a["shutdown_vehicle"]["reasons"] == [
        "NOT_READY"
    ]
    assert a["navigate_to_point"]["allowed"] is False
    assert a["stop_motion"]["allowed"] is False  # canonical safety path is not a v1 path
    # permission is still enforced per action
    doc2 = operation_readiness(
        R(), readiness, settings=S(), permissions={"robot.read"}, now=datetime.now(UTC)
    )
    assert doc2["actions"]["start_vehicle"]["allowed"] is False
    assert "PERMISSION_DENIED" in doc2["actions"]["start_vehicle"]["reasons"]


def test_r1c_snapshot_exposes_db_boot_id_for_readiness_binding(client) -> None:
    snap = client.get("/api/v1/monitor/snapshot").json()
    with SessionLocal() as db:
        boot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002")).boot_id
    robot = next(r for r in snap["robots"] if r["vehicle_id"] == "R002")
    assert robot["boot_id"] == boot
    assert robot["operation_readiness"]["boot_id"] == boot
