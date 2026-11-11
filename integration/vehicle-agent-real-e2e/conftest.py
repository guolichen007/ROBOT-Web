"""真实组件离线 E2E 环境编排。

链路：真实 Mosquitto（TLS + per-device 凭证 + 生产同款 ACL pattern）→ 现有
vehicle-agent-adapter → mqtt-ingress → PostgreSQL/Redis → 现有 FastAPI（TestClient）。
全部组件只连 127.0.0.1 测试端口；禁止连接任何生产 Broker。
要求 REAL_E2E=1（专用 CI job 与本地显式开启才会真实执行，杜绝误跑）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

# starlette 1.6 起 TestClient 依赖 httpx 被标记弃用（第三方库自身的迁移提示，
# 仓库 requirements 仍锁定 httpx==0.28.1 / starlette==1.6.0）。本测试环境沿用仓库
# 锁定版本组合，仅对本提示做局部过滤，以便在 -W error 严格模式下仍可运行；
# 不涉及任何安全或功能门禁的关闭。
warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).parent

VEHICLE = "firebot-vehicle-01"
VEHICLE_PASSWORD = "e2e-vehicle-01-password"
GHOST_VEHICLE = "firebot-vehicle-99"  # 有凭证、无云端资产（未知设备反证用）
PLATFORM_USER = "platform"
PLATFORM_PASSWORD = "e2e-platform-password"

E2E_ENV_DEFAULTS = {
    "APP_ENV": "test",
    "DATABASE_URL": "postgresql+psycopg://firebot_e2e:firebot_e2e@localhost:5432/firebot_e2e",
    "REDIS_URL": "redis://localhost:6379/15",
    "MQTT_HOST": "127.0.0.1",
    "MQTT_PORT": "18883",
    "MQTT_TLS": "1",
    "MQTT_USERNAME": PLATFORM_USER,
    "MQTT_PASSWORD": PLATFORM_PASSWORD,
    "JWT_SECRET": "e2e-jwt-secret-0123456789abcdef0123456789",
    "REFRESH_SECRET": "e2e-refresh-secret-0123456789abcdef012345",
    "CSRF_SECRET": "e2e-csrf-secret-0123456789abcdef0123456789",
    "BOOTSTRAP_ADMIN_PASSWORD": "E2e-Only-Admin-Password-2026!",
    "SEED_DEMO": "true",
    "ASSET_ROOT": "/tmp/firebot-e2e-assets",
    "VEHICLE_AGENT_V1_DOWNLINK_ENABLED": "false",
}

# 模块级强引用：长生命周期子进程与日志句柄在会话结束前绝不被 GC。
_STACK_PROCS: list = []
_STACK_HANDLES: list = []

ACL_TEMPLATE = """# E2E 测试 ACL（镜像生产模板：username==vehicle_id，%u pattern）
user {platform}
topic readwrite robot/+/+
topic readwrite _platform/compat/+/+
topic readwrite _platform/vehicle-agent-v1/+/+
topic readwrite v1/robot/+/+

user {vehicle}
pattern read v1/robot/%u/cmd
pattern write v1/robot/%u/ack
pattern write v1/robot/%u/status
pattern write v1/robot/%u/telemetry
pattern write v1/robot/%u/availability
pattern write v1/robot/%u/event

user {ghost}
pattern read v1/robot/%u/cmd
pattern write v1/robot/%u/ack
pattern write v1/robot/%u/status
pattern write v1/robot/%u/telemetry
pattern write v1/robot/%u/availability
pattern write v1/robot/%u/event
"""


def _apply_env() -> None:
    for key, value in E2E_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


def _port_open(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _spawn(name: str, args: list[str], env: dict, log_handle) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture(scope="session")
def e2e_stack(tmp_path_factory):
    if os.environ.get("REAL_E2E") != "1":
        pytest.skip("REAL_E2E=1 required（真实组件 E2E 仅在专用 job/显式开启时执行）")
    _apply_env()
    workdir = tmp_path_factory.mktemp("e2e")
    certs = workdir / "certs"
    generated = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/generate_readonly_test_certs.sh"), str(certs)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr

    # ---- Mosquitto：密码文件（mosquitto_passwd）+ ACL + TLS 配置 ----
    passwd = workdir / "passwd"
    passwd.touch()  # mosquitto_passwd -b 要求文件已存在（兼容 1.6/2.x，-c 与 -b 不共存）
    acl = workdir / "acl"
    acl.write_text(
        ACL_TEMPLATE.format(platform=PLATFORM_USER, vehicle=VEHICLE, ghost=GHOST_VEHICLE)
    )
    for user, password in (
        (PLATFORM_USER, PLATFORM_PASSWORD),
        (VEHICLE, VEHICLE_PASSWORD),
        (GHOST_VEHICLE, "e2e-ghost-password"),
    ):
        subprocess.run(
            ["mosquitto_passwd", "-b", str(passwd), user, password],
            check=True,
            capture_output=True,
        )
    mosquitto_conf = workdir / "mosquitto.e2e.conf"
    # 随机空闲端口：避免与孤儿进程/系统 mosquitto/并行运行冲突（固定端口曾导致
    # 新会话连上旧 broker、CA 签名不匹配的假故障）。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        mqtt_port = probe.getsockname()[1]
    os.environ["MQTT_PORT"] = str(mqtt_port)
    os.environ["MQTT_CA_FILE"] = str(certs / "ca.crt")
    mosquitto_conf.write_text(
        "\n".join(
            [
                f"listener {mqtt_port} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {passwd}",
                f"acl_file {acl}",
                f"cafile {certs / 'ca.crt'}",
                f"certfile {certs / 'server.crt'}",
                f"keyfile {certs / 'server.key'}",
                "require_certificate false",
                "persistence false",
                "log_dest stdout",
            ]
        )
        + "\n"
    )
    mosquitto_log = workdir / "mosquitto.log"
    # 日志句柄与长生命周期进程显式持有（模块级强引用），teardown 再统一关闭/回收，
    # 避免 pytest 在测试 teardown 阶段 GC 触发"subprocess still running"告警。
    log_handles = _STACK_HANDLES
    stack_procs = _STACK_PROCS
    mosquitto: subprocess.Popen | None = None
    adapter: subprocess.Popen | None = None
    ingress: subprocess.Popen | None = None
    try:
        mosquitto_handle = open(mosquitto_log, "ab")  # noqa: SIM115
        log_handles.append(mosquitto_handle)
        mosquitto = subprocess.Popen(
            ["mosquitto", "-c", str(mosquitto_conf)],
            stdout=mosquitto_handle,
            stderr=subprocess.STDOUT,
        )
        stack_procs.append(mosquitto)
        assert mosquitto.poll() is None, "mosquitto exited immediately"
        assert _port_open("127.0.0.1", mqtt_port, 15.0), "mosquitto did not listen"

        # ---- Adapter + Ingress（现有服务进程，TLS + platform 凭证）----
        env = dict(os.environ)
        # app 模块位于 apps/api；services 位于仓库根。与 pyproject pythonpath 配置一致。
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
        adapter_handle = open(workdir / "adapter.log", "ab")  # noqa: SIM115
        log_handles.append(adapter_handle)
        adapter = _spawn(
            "vehicle-agent-adapter",
            [sys.executable, "services/vehicle-agent-adapter/main.py"],
            env,
            adapter_handle,
        )
        stack_procs.append(adapter)
        ingress_handle = open(workdir / "ingress.log", "ab")  # noqa: SIM115
        log_handles.append(ingress_handle)
        ingress = _spawn(
            "mqtt-ingress",
            [sys.executable, "services/mqtt-ingress/main.py"],
            env,
            ingress_handle,
        )
        stack_procs.append(ingress)
        import redis

        redis_client = redis.Redis.from_url(os.environ["REDIS_URL"])

        def heartbeats_ready() -> bool:
            return bool(
                redis_client.get("service:vehicle-agent-adapter:heartbeat")
                and redis_client.get("service:mqtt-ingress:heartbeat")
            )

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not heartbeats_ready():
            time.sleep(0.5)
        assert heartbeats_ready(), (
            f"adapter/ingress heartbeats missing\n"
            f"adapter log:\n{(workdir / 'adapter.log').read_text()[-2000:]}"
            f"\ningress log:\n{(workdir / 'ingress.log').read_text()[-2000:]}"
        )

        yield {
            "workdir": workdir,
            "certs": certs,
            "mqtt_port": mqtt_port,
            "mosquitto_log": mosquitto_log,
            "vehicle_password": VEHICLE_PASSWORD,
            "platform_password": PLATFORM_PASSWORD,
            "ghost_password": "e2e-ghost-password",
            "redis": redis_client,
        }
    finally:
        # setup 中途失败也必须清理，杜绝孤儿进程占用端口/心跳。
        # 全路径回收：terminate → wait → kill → wait，任何异常都不中断后续清理。
        for proc in (adapter, ingress, mosquitto):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
            except ProcessLookupError:
                continue
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except ProcessLookupError:
                    pass
        for handle in log_handles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        # 注意：不清空 _STACK_PROCS/_STACK_HANDLES —— 保留模块级强引用直到进程退出
        # （仅 3 个对象），确保 Popen 不会在回收完成前被 GC 触发"still running"告警。
        # 关闭进程内 DB 连接池，避免 psycopg 连接在 GC 时才关闭（ResourceWarning）。
        try:
            from app.db.session import engine

            engine.dispose()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="session")
def vehicle_asset(e2e_stack):
    """创建仅用于测试的 firebot-vehicle-01 云端资产（不复用 R001/R002 历史数据）。

    并关闭 admin 初始密码强制修改（与 apps/api/tests 的 stable_baseline 同语义，
    仅测试环境；不影响任何生产行为）。
    """
    from app.db.models import Robot, Site, User
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None, "seed required before running real E2E"
        admin.must_change_password = False
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        assert site is not None, "seed required before running real E2E"
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        if robot is None:
            robot = Robot(
                vehicle_id=VEHICLE,
                name="firebot-001",
                site_id=site.id,
                agent_protocol="VEHICLE_AGENT_V1",
                enabled=True,
            )
            db.add(robot)
        db.commit()
        return robot


@pytest.fixture(scope="session")
def r002_snapshot(e2e_stack):
    """R002 会话前快照：E2E 全程不得变更（R002_UNCHANGED 证据）。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        assert robot is not None, "seed required before running real E2E"
        return {
            "id": robot.id,
            "vehicle_id": robot.vehicle_id,
            "agent_protocol": robot.agent_protocol,
            "online_state": robot.online_state,
            "boot_id": robot.boot_id,
            "agent_state_json": robot.agent_state_json,
            "battery": robot.battery,
            "current_mode": robot.current_mode,
            "updated_at": robot.updated_at,
        }


@pytest.fixture(scope="session")
def api_client(e2e_stack):
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def auth_headers(api_client):
    from app.core.config import get_settings

    response = api_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
