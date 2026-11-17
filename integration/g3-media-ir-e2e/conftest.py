"""G3 隔离真实组件 E2E：媒体（MediaMTX）RGB/thermal + 旧红外数值协议兼容性。

G3-R2：产品不再有独立温度数值来源。ir_telemetry_sim 仅在 run_legacy_thermal_sim
（兼容性测试）中以显式开关运行；Web/媒体验收的模拟设备在线状态只用 run_presence
（availability/status，零 telemetry）。

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


def add_broker_user(passwd: Path, user: str, password: str) -> None:
    """G3-R2 SECRET_LOG_REDACTION：口令经 stdin 交给 mosquitto_passwd，不进 argv。

    旧写法 ``mosquitto_passwd -b <file> <user> <password>`` + check=True 失败时，
    CalledProcessError 的 str() 携带完整 argv，pytest 失败日志会原样输出测试口令。
    """
    result = subprocess.run(
        ["mosquitto_passwd", str(passwd), user],
        input=f"{password}\n{password}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # 只报告用户名与退出码；stderr 也经掩码（防御：工具若回显口令）
        detail = (result.stderr or "").replace(password, "***")[-300:]
        raise RuntimeError(f"mosquitto_passwd failed for {user} rc={result.returncode}: {detail}")


def redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


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
        add_broker_user(passwd, user, password)
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


def _assert_sim_namespace(vehicle_id: str) -> None:
    """fixture 只允许创建/清理合成命名空间资产；真实车辆（R001/R002/firebot-vehicle-*）一律拒绝。"""
    from app.core.config import REAL_VEHICLE_IDS, is_synthetic_namespace

    if not is_synthetic_namespace(vehicle_id) or vehicle_id in REAL_VEHICLE_IDS:
        raise RuntimeError(f"G3 fixture refuses non-synthetic vehicle {vehicle_id!r}")


# 本次会话创建的资源（只清理这些；预先存在的同名合成资产保留原状）
CREATED: dict[str, set] = {"robots": set(), "streams": set()}


def _ensure_sim_robot():
    from app.db.models import Robot, Site
    from app.db.session import SessionLocal
    from sqlalchemy import select

    _assert_sim_namespace(SIM)
    with SessionLocal() as db:
        if not db.scalar(select(Robot).where(Robot.vehicle_id == SIM)):
            site = db.scalar(select(Site).order_by(Site.id).limit(1))
            robot = Robot(
                vehicle_id=SIM,
                name="SIMULATED IR 01",
                site_id=site.id,
                agent_protocol="VEHICLE_AGENT_V1",
                enabled=False,
            )
            db.add(robot)
            db.commit()
            CREATED["robots"].add(robot.id)


def _cleanup_sim_robot():
    """只删除本会话创建的模拟机器人及其派生行；预先存在的同名资产不动（只清理本次产生的流）。"""
    from app.db.models import Base, Robot
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        if robot and robot.id in CREATED["robots"]:
            _assert_sim_namespace(robot.vehicle_id)
            for table in reversed(Base.metadata.sorted_tables):
                if "robot_id" in table.c and table.name != "robots":
                    db.execute(table.delete().where(table.c.robot_id == robot.id))
            db.execute(delete(Robot).where(Robot.id == robot.id))
            db.commit()
            CREATED["robots"].discard(robot.id)


LEGACY_THERMAL_ENV = "FIREBOT_LEGACY_THERMAL_COMPAT_TEST"


def _run_module(g3, module: str, extra: tuple[str, ...], env_extra: dict | None = None) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", module, "--config", str(g3["identity"]), *extra],
        cwd=str(REPO_ROOT),
        env={**g3["base"], **(env_extra or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, redact_text(result.stderr[-500:], (SIM_PASSWORD,))
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_legacy_thermal_sim(g3, *extra: str) -> dict:
    """仅旧协议兼容性测试：显式开关下运行已停用的 ir_telemetry_sim（发送 telemetry.thermal）。"""
    return _run_module(
        g3,
        "services.ir_telemetry_sim.main",
        ("--legacy-compat-test", *extra),
        {LEGACY_THERMAL_ENV: "1"},
    )


# 兼容旧调用名：仍是“旧温度协议兼容性”专用
run_sim = run_legacy_thermal_sim


def run_presence(g3, *extra: str) -> dict:
    """G3-R2 产品路径：模拟设备只上报 availability/status，不发送任何 telemetry/温度。"""
    data = _run_module(g3, "services.sim_presence.main", extra)
    assert data["telemetry_sent"] == 0, data
    return data


def mediamtx_binary() -> str | None:
    return os.environ.get("G3_MEDIAMTX") or shutil.which("mediamtx")
