"""G3 IR 旧协议兼容性 E2E：模拟数值 → MQTT → Adapter/Ingress → PG/Redis/API。

G3-R2：产品已停用独立温度数值（无独立红外测温传感器）。本文件保留为**兼容性测试**——
云端仍须正确处理历史/兼容 telemetry.thermal（采集、新鲜度、类型、幂等、身份隔离、ACL），
不做破坏性迁移；模拟器只在显式开关下运行（conftest.run_legacy_thermal_sim）。
另含 G3-R2 新合同：模拟器默认停用；sim_presence 只上报在线状态、不发送任何 telemetry。
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import LEGACY_THERMAL_ENV, SIM, run_presence, run_sim, wait_for
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
        [
            sys.executable,
            "-m",
            "services.ir_telemetry_sim.main",
            "--legacy-compat-test",
            "--config",
            str(path),
        ],
        cwd=str(REPO_ROOT),
        env={**g3["base"], LEGACY_THERMAL_ENV: "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "must be firebot-sim-ir-01" in result.stderr


def test_sim_identity_cannot_touch_real_vehicle_telemetry(g3, api):
    """模拟身份独立：sim 的上行不会写入 firebot-vehicle-01；sim 账号也无法发布 001 topic（ACL）。"""
    import paho.mqtt.client as mqtt

    real = "firebot-vehicle-01"
    before = g3["redis"].get(f"robot:{real}:latest")
    run_sim(g3, "--values", "66.6", "--interval", "1.2")
    assert wait_for(lambda: latest(g3).get("top_ir") == 66.6, 15)
    # sim 凭证直接向 001 的 telemetry topic 发布：Broker ACL（%u）拒绝
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="g3-sim-spoof")
    identity = json.loads(g3["identity"].read_text())
    client.username_pw_set(identity["mqtt_username"], identity["mqtt_password"])
    client.tls_set(ca_certs=identity["ca_cert_path"])
    client.connect("127.0.0.1", g3["port"])
    client.loop_start()
    forged = {
        "thermal": {"max_temp": 99.9},
        "ts": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
    }
    client.publish(f"v1/robot/{real}/telemetry", json.dumps(forged), qos=1).wait_for_publish(5)
    time.sleep(2)
    client.loop_stop()
    client.disconnect()
    assert g3["redis"].get(f"robot:{real}:latest") == before  # 001 实时数据未被触碰
    from app.db.models import Robot, SensorSample
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == real))
        if robot is not None:
            hits = db.scalars(
                select(SensorSample).where(
                    SensorSample.robot_id == robot.id, SensorSample.top_ir_max.in_([66.6, 99.9])
                )
            ).all()
            assert not hits
    log = (g3["workdir"] / "mosquitto.log").read_text(errors="replace")
    assert "Denied PUBLISH" in log or "denied" in log.lower()


# ---------------- G3-R2 新合同：温度模拟停用 + 在线状态源零 telemetry ----------------


def _sim_sensor_count() -> int:
    return len(samples())


@pytest.mark.parametrize(
    "argv,env_flag",
    [
        ([], None),  # 默认：停用
        (["--legacy-compat-test"], None),  # 只有命令行开关：停用
        ([], "1"),  # 只有环境变量：停用
    ],
)
def test_temperature_simulator_disabled_by_default(g3, api, argv, env_flag):
    """G3-R2 TEMPERATURE_SIM_DISABLED：缺任一显式开关 → 退出码 3，零 MQTT 发布、零温度写入。"""
    import subprocess
    import sys

    from conftest import REPO_ROOT

    before_latest = g3["redis"].get(f"robot:{SIM}:latest")
    before_samples = _sim_sensor_count()
    env = {k: v for k, v in g3["base"].items() if k != LEGACY_THERMAL_ENV}
    if env_flag:
        env[LEGACY_THERMAL_ENV] = env_flag
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.ir_telemetry_sim.main",
            *argv,
            "--config",
            str(g3["identity"]),
            "--values",
            "88.8",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr[-300:]
    assert "disabled" in result.stderr and result.stdout == ""
    time.sleep(1.5)
    assert g3["redis"].get(f"robot:{SIM}:latest") == before_latest
    assert _sim_sensor_count() == before_samples and 88.8 not in samples()


def test_presence_reports_online_without_any_telemetry(g3, api):
    """G3-R2：模拟设备在线只靠 availability/status；不产生 telemetry / SensorSample / 温度变化。"""
    before_samples = _sim_sensor_count()
    before_top_ir = latest(g3).get("top_ir", "absent")
    before_state = snapshot_robot(api).get("agent_state") or {}
    before_telemetry_at = before_state.get("telemetry_received_at")
    data = run_presence(g3, "--hold-seconds", "2")
    assert data == {
        "vehicle_id": SIM,
        "simulated": True,
        "telemetry_sent": 0,
        "availability": True,
        "status": True,
    }
    assert wait_for(lambda: snapshot_robot(api)["online_state"] == "ONLINE", 15)
    robot = snapshot_robot(api)
    assert robot["simulation"]["simulated"] is True
    assert (robot.get("agent_state") or {}).get("telemetry_received_at") == before_telemetry_at
    assert _sim_sensor_count() == before_samples
    assert latest(g3).get("top_ir", "absent") == before_top_ir


def test_presence_refuses_real_vehicle_identity(g3, tmp_path):
    import subprocess
    import sys

    from conftest import REPO_ROOT

    for real in ("firebot-vehicle-01", "R002", "firebot-vehicle-03"):
        spoof = json.loads(g3["identity"].read_text())
        spoof["vehicle_id"] = spoof["mqtt_username"] = real
        path = tmp_path / f"spoof-{real}.json"
        path.write_text(json.dumps(spoof))
        path.chmod(0o600)
        result = subprocess.run(
            [sys.executable, "-m", "services.sim_presence.main", "--config", str(path)],
            cwd=str(REPO_ROOT),
            env=g3["base"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # 退出码 2：命名空间门禁（firebot-sim-<name>）或身份加载器既有冻结名单（R001/R002）先拒绝
        assert result.returncode == 2, (real, result.stderr[-200:])
        assert "refusing" in result.stderr or "identity rejected" in result.stderr, real
        assert result.stdout == "", real  # 未连接 Broker、未发布任何消息
