"""G3 隔离真实组件 E2E：红外模拟数值链路 + 媒体（MediaMTX）RGB/thermal。

复用 G2 E2E 的测试目标守卫（任何写入前校验隔离 DB/Redis）。所有组件只连 127.0.0.1 随机端口。
需要 REAL_E2E=1；媒体段另需 G3_MEDIAMTX（MediaMTX 二进制）与 ffmpeg，否则显式 skip。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "integration" / "g2-command-loop-e2e"))
SIM = "firebot-sim-ir-01"
SIM_PASSWORD = "g3-sim-ir-password"
PLATFORM_PASSWORD = "g3-platform-password"
_PROCS: list = []
_HANDLES: list = []

ACL = """user platform
topic readwrite _platform/vehicle-agent-v1/+/+
topic readwrite v1/robot/+/+

pattern read v1/robot/%u/cmd
pattern write v1/robot/%u/status
pattern write v1/robot/%u/telemetry
pattern write v1/robot/%u/availability
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for(predicate, timeout=20.0, interval=0.2):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


def spawn(workdir: Path, name: str, args: list[str], env: dict) -> subprocess.Popen:
    handle = open(workdir / f"{name}.log", "ab")  # noqa: SIM115
    _HANDLES.append(handle)
    proc = subprocess.Popen(
        args, cwd=str(REPO_ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    _PROCS.append(proc)
    return proc


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def g3(tmp_path_factory):
    if os.environ.get("REAL_E2E") != "1":
        pytest.skip("REAL_E2E=1 required")
    from target_guard import TargetGuardError, assert_isolated_target

    try:
        assert_isolated_target()
    except TargetGuardError as exc:
        pytest.exit(f"G3_E2E_TARGET_GUARD: {exc}", returncode=3)
    workdir = tmp_path_factory.mktemp("g3")
    certs = workdir / "certs"
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/generate_readonly_test_certs.sh"), str(certs)],
        check=True,
        capture_output=True,
    )
    passwd = workdir / "passwd"
    passwd.touch()
    for user, password in (("platform", PLATFORM_PASSWORD), (SIM, SIM_PASSWORD)):
        subprocess.run(
            ["mosquitto_passwd", "-b", str(passwd), user, password], check=True, capture_output=True
        )
    (workdir / "acl").write_text(ACL)
    port = free_port()
    (workdir / "mosquitto.conf").write_text(
        "\n".join(
            [
                f"listener {port} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {passwd}",
                f"acl_file {workdir / 'acl'}",
                f"cafile {certs / 'ca.crt'}",
                f"certfile {certs / 'server.crt'}",
                f"keyfile {certs / 'server.key'}",
                "require_certificate false",
                "persistence false",
                "log_type all",
                f"log_dest file {workdir / 'mosquitto.log'}",
            ]
        )
        + "\n"
    )
    spawn(
        workdir, "mosquitto", ["mosquitto", "-c", str(workdir / "mosquitto.conf")], dict(os.environ)
    )
    assert wait_port(port)
    os.environ.update(
        MQTT_HOST="127.0.0.1",
        MQTT_PORT=str(port),
        MQTT_TLS="1",
        MQTT_USERNAME="platform",
        MQTT_PASSWORD=PLATFORM_PASSWORD,
        MQTT_CA_FILE=str(certs / "ca.crt"),
        SIMULATED_VEHICLE_IDS=SIM,
        VEHICLE_AGENT_V1_DOWNLINK_ENABLED="false",
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    _ensure_sim_robot()
    base = dict(os.environ)
    base["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
    env = {**base, "FIREBOT_SERVICE_INSTANCE_ID": "g3-adapter", "FIREBOT_LEGACY_HEARTBEAT": "1"}
    spawn(workdir, "adapter", [sys.executable, "services/vehicle-agent-adapter/main.py"], env)
    spawn(
        workdir,
        "ingress",
        [sys.executable, "services/mqtt-ingress/main.py"],
        {**base, "FIREBOT_SERVICE_INSTANCE_ID": "g3-ingress"},
    )
    import redis as redis_lib

    redis = redis_lib.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    assert wait_for(
        lambda: redis.get("service:vehicle-agent-adapter:heartbeat")
        and redis.get("service:mqtt-ingress:heartbeat:g3-ingress"),
        40,
    ), "services not healthy"
    identity = workdir / "sim-identity.json"
    identity.write_text(
        json.dumps(
            {
                "vehicle_id": SIM,
                "display_name": "SIMULATED IR",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": port,
                "mqtt_username": SIM,
                "mqtt_password": SIM_PASSWORD,
                "ca_cert_path": str(certs / "ca.crt"),
            }
        )
    )
    identity.chmod(0o600)
    context = {"workdir": workdir, "redis": redis, "identity": identity, "port": port, "base": base}
    try:
        yield context
    finally:
        for proc in reversed(_PROCS):
            stop(proc)
        for handle in _HANDLES:
            handle.close()
        _cleanup_sim_robot()


def _ensure_sim_robot():
    from app.db.models import Robot, Site
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        if not db.scalar(select(Robot).where(Robot.vehicle_id == SIM)):
            site = db.scalar(select(Site).order_by(Site.id).limit(1))
            db.add(
                Robot(
                    vehicle_id=SIM,
                    name="SIMULATED IR 01",
                    site_id=site.id,
                    agent_protocol="VEHICLE_AGENT_V1",
                    enabled=False,
                )
            )
            db.commit()


def _cleanup_sim_robot():
    from app.db.models import Base, Robot
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        if robot:
            for table in reversed(Base.metadata.sorted_tables):
                if "robot_id" in table.c and table.name != "robots":
                    db.execute(table.delete().where(table.c.robot_id == robot.id))
            db.execute(delete(Robot).where(Robot.id == robot.id))
            db.commit()


def run_sim(g3, *extra: str) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.ir_telemetry_sim.main",
            "--config",
            str(g3["identity"]),
            *extra,
        ],
        cwd=str(REPO_ROOT),
        env=g3["base"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def mediamtx_binary() -> str | None:
    return os.environ.get("G3_MEDIAMTX") or shutil.which("mediamtx")
