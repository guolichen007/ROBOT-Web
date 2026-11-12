"""G3 IR 真实组件 E2E：模拟数值 → MQTT → Adapter/Ingress → PG/Redis/API。"""

from __future__ import annotations

import json
import time

import pytest
from conftest import SIM, run_sim, wait_for
from sqlalchemy import select


@pytest.fixture(scope="module")
def api(g3):
    from app.main import app
    from conftest_g2_admin import admin_client

    with admin_client(app) as client:
        yield client


def latest(g3):
    return json.loads(g3["redis"].get(f"robot:{SIM}:latest") or "{}")


def samples():
    from app.db.models import Robot, SensorSample
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        rows = db.scalars(
            select(SensorSample)
            .where(SensorSample.robot_id == robot.id)
            .order_by(SensorSample.server_received_at)
        ).all()
        return [row.top_ir_max for row in rows]


def snapshot_robot(api):
    robots = api.get("/api/v1/monitor/snapshot").json()["robots"]
    return next(r for r in robots if r["vehicle_id"] == SIM)


EVIDENCE: list = []


@pytest.mark.parametrize("value", [27.5, 42.0, 67.8])
def test_value_reaches_sensor_sample_redis_and_api(g3, api, value):
    run_sim(g3, "--values", str(value), "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir") == value, 15), latest(g3)
    assert wait_for(lambda: value in samples(), 10), samples()
    robot = snapshot_robot(api)
    assert robot["top_ir"] == value
    assert robot["agent_state"]["telemetry"]["thermal"]["max_temp"] == value
    EVIDENCE.append(
        {"sent": value, "redis_top_ir": latest(g3)["top_ir"], "api_top_ir": robot["top_ir"]}
    )


def test_thermal_null_is_unknown(g3, api):
    run_sim(g3, "--values", "42.0", "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir") == 42.0, 15)
    run_sim(g3, "--values", "null", "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir", "x") is None, 15), latest(g3)
    assert snapshot_robot(api)["top_ir"] is None


def test_stale_10_minutes_rejected(g3, api):
    run_sim(g3, "--values", "42.0", "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir") == 42.0, 15)
    before = len(samples())
    run_sim(g3, "--values", "99.9", "--stale-seconds", "600", "--interval", "1.5")
    time.sleep(2)
    assert latest(g3)["top_ir"] == 42.0
    assert 99.9 not in samples() and len(samples()) == before
    assert snapshot_robot(api)["top_ir"] == 42.0
    log = (g3["workdir"] / "ingress.log").read_text()
    assert "telemetry not applied as live" in log


@pytest.mark.parametrize(
    "raw", ['{"max_temp": "hot"}', '{"max_temp": true}', '{"max_temp": "42.0"}']
)
def test_invalid_type_rejected(g3, api, raw):
    run_sim(g3, "--values", "42.0", "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir") == 42.0, 15)
    run_sim(g3, "--raw-thermal", raw)
    time.sleep(2)
    assert latest(g3)["top_ir"] == 42.0
    assert "adapter rejected message" in (g3["workdir"] / "adapter.log").read_text()


def test_simulated_label_server_side_only(g3, api):
    robot = snapshot_robot(api)
    assert robot["simulation"] == {
        "simulated": True,
        "label": "SIMULATED",
        "source": "simulated_vehicle_ids",
    }
    others = [
        r for r in api.get("/api/v1/monitor/snapshot").json()["robots"] if r["vehicle_id"] != SIM
    ]
    assert others and all(r.get("simulation") is None for r in others)
    # MOCK 元数据不会自动成为 V1 模拟标识：R001（seed 的 MOCK 测试车）仍为 None
    assert next(r for r in others if r["vehicle_id"] == "R001")["simulation"] is None
    print("G3_IR_EVIDENCE " + json.dumps(EVIDENCE))


def test_simulator_refuses_real_vehicle_identity(g3, tmp_path):
    import subprocess
    import sys

    from conftest import REPO_ROOT

    spoof = json.loads(g3["identity"].read_text())
    spoof["vehicle_id"] = spoof["mqtt_username"] = "firebot-vehicle-01"
    path = tmp_path / "spoof.json"
    path.write_text(json.dumps(spoof))
    path.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-m", "services.ir_telemetry_sim.main", "--config", str(path)],
        cwd=str(REPO_ROOT),
        env=g3["base"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "must be firebot-sim-ir-01" in result.stderr
