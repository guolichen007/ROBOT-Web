"""WEB-R1C R1C-4: SIM ingress isolation (real PostgreSQL). The runtime chain is in
scripts/r1c_sim_chain.py; here: the namespace is inert outside SIM, REAL/mismatching vehicles
are refused, and a reboot voids the confirmed scene."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.db.models import Robot
from app.db.session import SessionLocal
from sqlalchemy import select

from services import sim_v21


def _fb(vehicle: str, env: str = "SIM") -> bytes:
    return json.dumps(
        {
            "contract_version": "2.1.0-draft.3",
            "kind": "MissionFeedback",
            "vehicle_id": vehicle,
            "environment": env,
            "boot_id": str(uuid4()),
            "seq": 1,
            "mission_id": str(uuid4()),
            "command_id": str(uuid4()),
            "execution_gen": 1,
            "goal_id": None,
            "state": "ACCEPTED",
            "phase": "ACCEPTED",
            "progress": 0,
            "waypoint_index": 0,
            "waypoint_total": 2,
            "current_point_id": None,
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ).encode()


def _counts() -> dict:
    from app.core.events import get_redis

    raw = get_redis().hgetall("metrics:sim_v21")
    return {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}


def test_namespace_is_inert_in_a_real_deployment(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "firebot_deployment_env", "REAL")
    before = _counts()
    assert sim_v21.process(
        "_sim/v21/firebot-sim-r002-01/mission_feedback", _fb("firebot-sim-r002-01")
    )
    assert _counts() == before  # consumed and ignored: nothing parsed, nothing written


def test_other_topics_are_not_claimed() -> None:
    assert sim_v21.process("robot/R001/location", b"{}") is False


@pytest.mark.parametrize(
    ("vehicle", "env", "code"),
    [("R002", "SIM", "ENV_MISMATCH"), ("R002", "REAL", "ENV_MISMATCH")],
)
def test_real_vehicle_feedback_refused_even_in_sim(monkeypatch, vehicle, env, code) -> None:
    monkeypatch.setattr(get_settings(), "firebot_deployment_env", "SIM")
    before = _counts().get(code, 0)
    sim_v21.process(f"_sim/v21/{vehicle}/mission_feedback", _fb(vehicle, env))
    assert _counts().get(code, 0) == before + 1


def test_topic_payload_vehicle_mismatch_refused(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "firebot_deployment_env", "SIM")
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        robot.vehicle_id = "firebot-sim-x-1"  # in-memory double, never committed
        with pytest.raises(sim_v21.SimIngressRefused) as err:
            sim_v21._guard(robot, {"environment": "SIM", "vehicle_id": "firebot-sim-x-2"})
        db.rollback()
    assert err.value.code == "VEHICLE_MISMATCH"


def test_reboot_voids_the_confirmed_scene() -> None:
    class R:
        loaded_scene_version_id = "v"
        loaded_scene_digest = "d" * 64
        loaded_scene_boot_id = "b"

    r = R()
    sim_v21.clear_loaded_scene(r)  # type: ignore[arg-type]
    assert (r.loaded_scene_version_id, r.loaded_scene_digest, r.loaded_scene_boot_id) == (
        None,
        None,
        None,
    )
