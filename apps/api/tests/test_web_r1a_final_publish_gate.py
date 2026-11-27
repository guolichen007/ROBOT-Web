"""WEB-R1A 最终发布门禁反证：三入口（Outbox / Redis 人工控制 / 安全 Stream）统一拒绝。

每个用例都断言 MQTT 发布次数（捕获客户端），而不是只看返回值：
拒绝 = 0 次 publish + 终态记录（Outbox REJECTED / Command GATE_REJECTED / 拒绝计数）。
正向对照只存在于 SIM 命名空间车辆 + SIM 环境 + 显式白名单；REAL 默认拒绝。
VEHICLE_AGENT_V1（001）分支不经过本门禁，其行为由既有 V1 测试覆盖、不变。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.db.models import Command, OutboxEvent, Robot, User
from app.db.session import SessionLocal
from app.modules.commands.service import build_command_payload
from fakeredis import FakeRedis
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[3]
SIM_ID = "firebot-sim-r1a-gate-01"
OTHER_SIM_ID = "firebot-sim-r1a-gate-02"


def _load_dispatcher():
    spec = importlib.util.spec_from_file_location(
        "firebot_dispatcher_r1a_gate", ROOT / "services" / "command-dispatcher" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Capture:
    """Stands in for the paho client: records every publish that actually happens."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.fail_next = False

    def is_connected(self) -> bool:
        return True

    def publish(self, topic, payload, qos=1, retain=False, properties=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("broker down")
        self.published.append((topic, json.loads(payload)))

        class Info:
            def wait_for_publish(self, timeout=3):
                return None

            def is_published(self):
                return True

        return Info()


@pytest.fixture(scope="module")
def disp():
    return _load_dispatcher()


@pytest.fixture
def env(disp, monkeypatch):
    """Fresh capture client + fake redis + SIM robots; settings restored afterwards."""
    cap = Capture()
    fake = FakeRedis(decode_responses=True)
    monkeypatch.setattr(disp, "client", cap)
    monkeypatch.setattr(disp, "redis", fake)
    settings = get_settings()
    for key, value in {
        "firebot_deployment_env": "SIM",
        "canonical_downlink_allowlist": SIM_ID,
        "downlink_frozen": False,
        "mqtt_host": "127.0.0.1",
    }.items():
        monkeypatch.setattr(settings, key, value)
    with SessionLocal.begin() as db:
        r002 = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert r002 and admin
        r002.boot_id = r002.boot_id or str(uuid4())
        ids = {}
        for vid in (SIM_ID, OTHER_SIM_ID):
            db.execute(delete(Robot).where(Robot.vehicle_id == vid))
            robot = Robot(
                vehicle_id=vid,
                site_id=r002.site_id,
                name=vid,
                agent_protocol="CANONICAL_MQTT",
                boot_id=str(uuid4()),
            )
            db.add(robot)
            db.flush()
            ids[vid] = robot.id
        state = {"admin": admin.id, "robots": ids, "r002": r002.id}
    yield {"cap": cap, "redis": fake, "settings": settings, **state}
    with SessionLocal.begin() as db:
        robot_ids = list(ids.values()) + [state["r002"]]
        cmd_ids = db.scalars(
            select(Command.command_id).where(Command.robot_id.in_(robot_ids))
        ).all()
        if cmd_ids:
            db.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(cmd_ids)))
            db.execute(delete(Command).where(Command.command_id.in_(cmd_ids)))
        db.execute(delete(Robot).where(Robot.id.in_(list(ids.values()))))


def _robot(vehicle_id: str) -> Robot:
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        db.expunge(robot)
        return robot


def _payload(env, vehicle_id=SIM_ID, cmd="return_dock", ttl_ms=30_000, **kw):
    return build_command_payload(
        robot=_robot(vehicle_id),
        operator_id=env["admin"],
        cmd=cmd,
        params=kw.pop("params", {}),
        ttl_ms=ttl_ms,
        priority=50,
        **kw,
    )


def _persist(payload, *, lifecycle="QUEUED", outbox=True, robot_vehicle=None, outbox_payload=None):
    """Mirror app.modules.commands.service: Command row (+ Outbox event)."""
    with SessionLocal.begin() as db:
        robot = db.scalar(
            select(Robot).where(Robot.vehicle_id == (robot_vehicle or payload["vehicle_id"]))
        )
        db.add(
            Command(
                command_id=payload["command_id"],
                correlation_id=payload["correlation_id"],
                robot_id=robot.id,
                cmd=payload["cmd"],
                priority=50,
                payload_json=payload,
                lifecycle_status=lifecycle,
                issued_by=payload["operator_id"],
                issued_at=datetime.fromisoformat(payload["issued_at"]),
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
        )
        db.flush()
        if outbox:
            db.add(
                OutboxEvent(
                    aggregate_type="COMMAND",
                    aggregate_id=payload["command_id"],
                    event_type="COMMAND_PUBLISH",
                    payload_json=outbox_payload or payload,
                )
            )


def _forge_outbox(payload) -> None:
    with SessionLocal.begin() as db:
        db.add(
            OutboxEvent(
                aggregate_type="COMMAND",
                aggregate_id=payload["command_id"],
                event_type="COMMAND_PUBLISH",
                payload_json=payload,
            )
        )


def _state(command_id: str) -> tuple[str | None, str | None, str | None]:
    with SessionLocal() as db:
        row = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == command_id))
        cmd = db.scalar(select(Command).where(Command.command_id == command_id))
        return (
            row.status if row else None,
            cmd.lifecycle_status if cmd else None,
            cmd.ack_reason if cmd else None,
        )


def _drain(disp) -> None:
    for _ in range(20):
        if not disp.dispatch_next():
            break


def _denied(env, path: str, code: str) -> int:
    return int(env["redis"].get(f"dispatch:fg:denied:{path}:{code}") or 0)


# ------------------------------------------------------------------ Outbox entry point


def test_outbox_sim_authorised_positive_control(disp, env) -> None:
    payload = _payload(env)
    _persist(payload)
    _drain(disp)
    assert [t for t, _ in env["cap"].published] == [f"robot/{SIM_ID}/command"]
    assert _state(payload["command_id"])[:2] == ("PUBLISHED", "PUBLISHED")


def test_outbox_real_vehicle_default_deny(disp, env, monkeypatch) -> None:
    monkeypatch.setattr(env["settings"], "firebot_deployment_env", "REAL")
    payload = _payload(env, vehicle_id="R002")
    _persist(payload)
    _drain(disp)
    assert env["cap"].published == []
    assert _state(payload["command_id"]) == ("REJECTED", "GATE_REJECTED", "FG_NOT_ALLOWLISTED")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("forged_outbox_no_command", "FG_UNAUTHORIZED"),
        ("tampered_outbox_payload", "FG_UNAUTHORIZED"),
        ("command_row_other_robot", "FG_UNAUTHORIZED"),
        ("wrong_boot", "FG_BOOT_MISMATCH"),
        ("not_allowlisted_sim", "FG_NOT_ALLOWLISTED"),
        ("frozen", "FG_DOWNLINK_FROZEN"),
        ("real_env_sim_vehicle", "FG_ENV_MISMATCH"),
        ("sim_env_real_vehicle", "FG_ENV_MISMATCH"),
        ("sim_env_production_broker", "FG_ENV_CREDENTIALS"),
        ("ttl_too_long", "FG_TTL_INVALID"),
    ],
)
def test_outbox_rejections_never_publish(disp, env, monkeypatch, mutation, code) -> None:
    s = env["settings"]
    vehicle = SIM_ID
    if mutation == "not_allowlisted_sim":
        vehicle = OTHER_SIM_ID
    if mutation == "sim_env_real_vehicle":
        vehicle = "R002"
        monkeypatch.setattr(s, "canonical_downlink_allowlist", f"{SIM_ID},R002")
    payload = _payload(
        env, vehicle_id=vehicle, ttl_ms=300_000 if mutation == "ttl_too_long" else 30_000
    )
    if mutation == "forged_outbox_no_command":
        _forge_outbox(payload)
    elif mutation == "tampered_outbox_payload":
        _persist(payload, outbox_payload={**payload, "params": {"speed": 9}})
    elif mutation == "command_row_other_robot":
        _persist(payload, robot_vehicle=OTHER_SIM_ID)
    elif mutation == "wrong_boot":
        _persist({**payload, "target_boot_id": str(uuid4())})
    else:
        _persist(payload)
    if mutation == "frozen":
        monkeypatch.setattr(s, "downlink_frozen", True)
    if mutation == "real_env_sim_vehicle":
        monkeypatch.setattr(s, "firebot_deployment_env", "REAL")
    if mutation == "sim_env_production_broker":
        monkeypatch.setattr(s, "mqtt_host", "firebot.youdaozd.com")
    _drain(disp)
    assert env["cap"].published == [], mutation
    assert _denied(env, "outbox", code) == 1, (
        mutation,
        dict(env["redis"].scan_iter("dispatch:fg:*")),
    )


def test_outbox_expired_is_terminal_and_never_published(disp, env) -> None:
    payload = _payload(env, ttl_ms=1)
    _persist(payload)
    import time

    time.sleep(0.05)
    _drain(disp)
    assert env["cap"].published == []
    assert _state(payload["command_id"])[:2] == ("EXPIRED", "EXPIRED")


def test_replay_of_an_already_published_command_is_refused(disp, env) -> None:
    payload = _payload(env)
    _persist(payload, outbox=False)
    disp.publish_command(payload, qos=1)  # first publish consumes the replay claim
    from services.dispatch_gate import GateDenied

    with pytest.raises(GateDenied, match="FG_REPLAY"):
        disp.publish_command(payload, qos=1)
    assert len(env["cap"].published) == 1


def test_broker_failure_releases_claim_so_retry_is_not_a_replay(disp, env) -> None:
    payload = _payload(env)
    _persist(payload, outbox=False)
    env["cap"].fail_next = True
    with pytest.raises(RuntimeError, match="broker down"):
        disp.publish_command(payload, qos=1)
    disp.publish_command(payload, qos=1)
    assert len(env["cap"].published) == 1


# ------------------------------------------------------------------ Redis safety Stream


def _safety(disp, payload) -> None:
    disp.process_safety_events([("1-0", {"command": json.dumps(payload)})])


def test_safety_stream_sim_authorised_positive_control(disp, env) -> None:
    payload = _payload(env, cmd="stop_motion", ttl_ms=3000)
    _persist(payload, lifecycle="CREATED", outbox=False)
    _safety(disp, payload)
    assert len(env["cap"].published) == 1
    assert _state(payload["command_id"])[1] == "PUBLISHED"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("forged_no_command", "FG_UNAUTHORIZED"),
        ("real_vehicle_estop", "FG_NOT_ALLOWLISTED"),
        ("wrong_boot", "FG_BOOT_MISMATCH"),
        ("manual_frame_injected_into_stream", "FG_UNAUTHORIZED"),
    ],
)
def test_safety_stream_rejections_never_publish(disp, env, monkeypatch, mutation, code) -> None:
    if mutation == "real_vehicle_estop":
        monkeypatch.setattr(env["settings"], "firebot_deployment_env", "REAL")
        payload = _payload(env, vehicle_id="R002", cmd="emergency_stop", ttl_ms=3000)
        _persist(payload, lifecycle="CREATED", outbox=False)
    elif mutation == "forged_no_command":
        payload = _payload(env, cmd="stop_motion", ttl_ms=3000)
    elif mutation == "wrong_boot":
        payload = {**_payload(env, cmd="stop_motion", ttl_ms=3000), "target_boot_id": str(uuid4())}
        _persist(payload, lifecycle="CREATED", outbox=False)
    else:
        payload = _manual(env)
    _safety(disp, payload)
    assert env["cap"].published == [], mutation
    assert _denied(env, "safety", code) == 1, mutation
    if mutation in ("real_vehicle_estop", "wrong_boot"):
        assert _state(payload["command_id"])[1:] == ("GATE_REJECTED", code)


# ------------------------------------------------------------------ Redis manual-control pubsub


def _manual(env, *, lease_user=None, vehicle=SIM_ID, set_lease=True):
    lease_id, session_id = str(uuid4()), str(uuid4())
    robot_id = env["robots"].get(vehicle)
    if set_lease and robot_id:
        env["redis"].set(
            f"manual:lease:{robot_id}",
            json.dumps(
                {
                    "lease_id": lease_id,
                    "user_id": lease_user or env["admin"],
                    "control_session_id": session_id,
                }
            ),
        )
    return _payload(
        env,
        vehicle_id=vehicle,
        cmd="manual_control",
        ttl_ms=500,
        params={"linear_x": 0.1, "angular_z": 0.0},
        lease_id=lease_id,
        control_session_id=session_id,
        seq=1,
    )


def test_manual_sim_with_matching_lease_positive_control(disp, env) -> None:
    disp.publish_command(_manual(env), qos=0, path="manual")
    assert len(env["cap"].published) == 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("no_lease", "FG_UNAUTHORIZED"),
        ("lease_of_other_user", "FG_UNAUTHORIZED"),
        ("non_manual_cmd_on_manual_path", "FG_UNAUTHORIZED"),
        ("real_vehicle", "FG_ENV_MISMATCH"),
    ],
)
def test_manual_rejections_never_publish(disp, env, mutation, code) -> None:
    from services.dispatch_gate import GateDenied

    if mutation == "no_lease":
        payload = _manual(env, set_lease=False)
    elif mutation == "lease_of_other_user":
        payload = _manual(env, lease_user=str(uuid4()))
    elif mutation == "non_manual_cmd_on_manual_path":
        payload = _payload(env, cmd="return_dock")
        _persist(payload, outbox=False)
    else:
        payload = _manual(env, vehicle="R002", set_lease=False)
    with pytest.raises(GateDenied, match=code):
        disp.publish_command(payload, qos=0, path="manual")
    assert env["cap"].published == []
    assert _denied(env, "manual", code) == 1


def test_manual_frame_through_outbox_path_refused(disp, env) -> None:
    payload = _manual(env)
    _forge_outbox(payload)
    _drain(disp)
    assert env["cap"].published == []
    assert _denied(env, "outbox", "FG_UNAUTHORIZED") == 1


def test_real_default_settings_deny_everything() -> None:
    """Shipped defaults: REAL env + empty canonical allowlist = no canonical vehicle eligible."""
    from app.core.config import Settings

    defaults = Settings.model_fields
    assert defaults["firebot_deployment_env"].default == "REAL"
    assert defaults["canonical_downlink_allowlist"].default == ""
    assert defaults["downlink_frozen"].default is False
    assert "firebot-vehicle-01" not in get_settings().canonical_downlink_allowlist_set
