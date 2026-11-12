"""G2 真实组件闭环 E2E：Web API → Outbox → Dispatcher（双 Broker 路由）→ r001 Broker →
专属 Adapter → V1 执行器（车端进程）→ ROS1 软件桥 → ROS 软件节点 → 反馈/ACK → 专属
Ingress → PostgreSQL → API。

拓扑（全部 127.0.0.1 随机端口，TLS + per-device 凭证 + 生产同款 ACL）：
  Broker A（原 Broker，platform 口令 A）：原 Adapter + 原 Ingress（default profile）
  Broker B（r001 独立 Broker，platform 口令 B ≠ A）：专属 Adapter + 专属 Ingress
  Dispatcher（唯一 Outbox 认领者）：default→A，r001-independent-v1→B（口令 B 文件）
  firebot-vehicle-01：V1 执行器（execution_grant=ros-software-test）连 Broker B
  ROS1：rosmaster + software_bridge + 001 正式 command_node.py（firebot_001_software 自定义消息）

需要 REAL_E2E=1 + 已迁移/seed 的 PG + Redis + mosquitto；ROS 段需要系统 python3 可导入
rospy（G2_ROSPY_PYTHONPATH 可指定），否则 ROS 用例显式 skip（不伪造通过）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VEHICLE = "firebot-vehicle-01"
VEHICLE_PASSWORD = "g2-vehicle-01-password"
PLATFORM_PASSWORD_A = "g2-platform-A-password"
PLATFORM_PASSWORD_B = "g2-platform-B-password-independent"
PROFILE = "r001-independent-v1"
DEDICATED_ADAPTER = "r001-dedicated-adapter"
DEDICATED_INGRESS = "r001-dedicated-ingress"
ROS_NAMESPACE = "/firebot_001/software"
ACK_TIMEOUT = 8

G2_ENV = {
    "APP_ENV": "test",
    "DATABASE_URL": "postgresql+psycopg://firebot_e2e:firebot_e2e@localhost:5432/firebot_e2e",
    "REDIS_URL": "redis://localhost:6379/14",
    "MQTT_HOST": "127.0.0.1",
    "MQTT_TLS": "1",
    "MQTT_USERNAME": "platform",
    "MQTT_PASSWORD": PLATFORM_PASSWORD_A,
    "JWT_SECRET": "e2e-jwt-secret-0123456789abcdef0123456789",
    "REFRESH_SECRET": "e2e-refresh-secret-0123456789abcdef012345",
    "CSRF_SECRET": "e2e-csrf-secret-0123456789abcdef0123456789",
    "SEED_DEMO": "true",
    "ASSET_ROOT": "/tmp/firebot-g2-e2e-assets",
    # 三侧一致的 G2 配置（API / Dispatcher / Adapter 读取同一组键）
    "VEHICLE_AGENT_V1_DOWNLINK_ENABLED": "true",
    "VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST": VEHICLE,
    "VEHICLE_TRANSPORT_PROFILES": f"{VEHICLE}={PROFILE}",
    "TRANSPORT_PROFILE_ADAPTERS": f"{PROFILE}={DEDICATED_ADAPTER}",
    "VEHICLE_EXECUTION_PROFILES": f"{VEHICLE}=ros-software-test",
    "VEHICLE_AGENT_V1_ACK_TIMEOUT_SECONDS": str(ACK_TIMEOUT),
}

ACL = """user platform
topic readwrite robot/+/+
topic readwrite _platform/compat/+/+
topic readwrite _platform/vehicle-agent-v1/+/+
topic readwrite v1/robot/+/+

pattern read v1/robot/%u/cmd
pattern write v1/robot/%u/ack
pattern write v1/robot/%u/status
pattern write v1/robot/%u/telemetry
pattern write v1/robot/%u/availability
pattern write v1/robot/%u/event
"""

_PROCS: list = []
_HANDLES: list = []


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


def spawn(workdir: Path, name: str, args: list[str], env: dict) -> subprocess.Popen:
    handle = open(workdir / f"{name}.log", "ab")  # noqa: SIM115
    _HANDLES.append(handle)
    proc = subprocess.Popen(
        args, cwd=str(REPO_ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    _PROCS.append(proc)
    return proc


def stop(proc: subprocess.Popen | None, sig_kill: bool = False) -> None:
    if proc is None or proc.poll() is not None:
        return
    (proc.kill if sig_kill else proc.terminate)()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def start_broker(workdir: Path, name: str, certs: Path, platform_password: str) -> int:
    passwd = workdir / f"passwd.{name}"
    passwd.touch()
    for user, password in (("platform", platform_password), (VEHICLE, VEHICLE_PASSWORD)):
        subprocess.run(
            ["mosquitto_passwd", "-b", str(passwd), user, password], check=True, capture_output=True
        )
    acl = workdir / f"acl.{name}"
    acl.write_text(ACL)
    port = free_port()
    conf = workdir / f"mosquitto.{name}.conf"
    conf.write_text(
        "\n".join(
            [
                f"listener {port} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {passwd}",
                f"acl_file {acl}",
                f"cafile {certs / 'ca.crt'}",
                f"certfile {certs / 'server.crt'}",
                f"keyfile {certs / 'server.key'}",
                "require_certificate false",
                "persistence false",
                "log_dest stdout",
                "log_type all",
            ]
        )
        + "\n"
    )
    spawn(workdir, f"mosquitto.{name}", ["mosquitto", "-c", str(conf)], dict(os.environ))
    assert wait_port(port), f"broker {name} did not listen"
    return port


def rospy_python() -> tuple[str, dict] | None:
    """返回 (python3 路径, env) 若系统 python3 可导入 rospy；否则 None。"""
    python = shutil.which("python3")
    if not python:
        return None
    env = dict(os.environ)
    extra = os.environ.get("G2_ROSPY_PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), extra]))
    env.setdefault("ROS_IP", "127.0.0.1")
    probe = subprocess.run(
        [python, "-c", "import rospy, std_msgs.msg"], env=env, capture_output=True, timeout=30
    )
    return (python, env) if probe.returncode == 0 else None


FIREBOT_001_PKG = REPO_ROOT / "integration" / "ros1" / "firebot-001-software"


def generate_001_messages(python3: str, env: dict, workdir: Path) -> Path:
    """用 genpy 为 firebot_001_software 生成 Python 消息类（与 catkin generate_messages 同源）。"""
    root = workdir / "rosmsg"
    out = root / "firebot_001_software" / "msg"
    out.mkdir(parents=True, exist_ok=True)
    (root / "firebot_001_software" / "__init__.py").write_text("")
    genmsg = os.environ.get("G2_GENMSG_PY") or shutil.which("genmsg_py.py") or "genmsg_py.py"
    for name in ("SoftwareCommand", "SoftwareFeedback", "SoftwareStatus"):
        subprocess.run(
            [
                python3,
                genmsg,
                "-p",
                "firebot_001_software",
                "-o",
                str(out),
                "-I",
                f"firebot_001_software:{FIREBOT_001_PKG / 'msg'}",
                str(FIREBOT_001_PKG / "msg" / f"{name}.msg"),
            ],
            check=True,
            env=env,
            capture_output=True,
        )
    subprocess.run(
        [python3, genmsg, "--initpy", "-p", "firebot_001_software", "-o", str(out)],
        check=True,
        env=env,
        capture_output=True,
    )
    return root


class Sniffer:
    """以 platform 身份旁听 Broker 上的命令 topic（证据：哪个 Broker 出现了哪条命令）。"""

    def __init__(self, port: int, password: str, ca: Path, topics: list[str]):
        import paho.mqtt.client as mqtt

        self.messages: list[tuple[str, bytes, bool]] = []
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"g2-sniffer-{port}", protocol=mqtt.MQTTv5
        )
        self.client.username_pw_set("platform", password)
        self.client.tls_set(ca_certs=str(ca))
        self.ready = False

        def on_connect(client, userdata, flags, rc, props):
            for name in topics:
                client.subscribe(name, qos=1)
            self.ready = True

        self.client.on_connect = on_connect
        self.client.on_message = lambda c, u, m: self.messages.append(
            (m.topic, bytes(m.payload), m.retain)
        )
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        deadline = time.monotonic() + 10
        while not self.ready and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)

    def topics(self) -> list[str]:
        return [name for name, _, _ in self.messages]

    def publish(self, topic: str, payload: dict) -> None:
        self.client.publish(topic, json.dumps(payload), qos=1).wait_for_publish(5)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


@pytest.fixture(scope="session")
def g2(tmp_path_factory):
    if os.environ.get("REAL_E2E") != "1":
        pytest.skip("REAL_E2E=1 required（真实组件 E2E 仅在显式开启时执行）")
    # 连接串与密钥沿用外部（CI job）配置；只覆盖 G2 路由/MQTT 测试键。
    # 不覆盖 BOOTSTRAP_ADMIN_PASSWORD（曾导致与 CI seed 的 admin 哈希不一致 → 401）。
    for key, value in G2_ENV.items():
        if key in {"DATABASE_URL", "REDIS_URL", "JWT_SECRET", "REFRESH_SECRET", "CSRF_SECRET"}:
            os.environ.setdefault(key, value)
        else:
            os.environ[key] = value
    workdir = tmp_path_factory.mktemp("g2")
    certs = workdir / "certs"
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/generate_readonly_test_certs.sh"), str(certs)],
        check=True,
        capture_output=True,
    )
    os.environ["MQTT_CA_FILE"] = str(certs / "ca.crt")
    port_a = start_broker(workdir, "A", certs, PLATFORM_PASSWORD_A)
    port_b = start_broker(workdir, "B", certs, PLATFORM_PASSWORD_B)
    os.environ["MQTT_PORT"] = str(port_a)

    # 测试库/缓存状态：清理 Redis 测试库；001 资产置为生产同款基线
    import redis as redis_lib

    redis = redis_lib.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    redis.flushdb()
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    # 进程内 API（TestClient）与子进程服务读取同一组环境变量（三侧一致）
    assert settings.vehicle_downlink_allowed(VEHICLE)
    assert settings.mqtt_port == port_a
    original = _snapshot_vehicle_asset()
    _prepare_vehicle_asset()

    platform_b = workdir / "r001-platform-pw"
    platform_b.write_text(PLATFORM_PASSWORD_B)
    platform_b.chmod(0o600)
    profiles = workdir / "dispatcher-broker-profiles.json"
    profiles.write_text(
        json.dumps(
            {
                PROFILE: {
                    "host": "127.0.0.1",
                    "port": port_b,
                    "username": "platform",
                    "password_file": str(platform_b),
                    "ca_file": str(certs / "ca.crt"),
                }
            }
        )
    )
    base = dict(os.environ)
    base["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
    py = sys.executable
    env_a = {
        **base,
        "FIREBOT_LEGACY_HEARTBEAT": "1",
        "FIREBOT_SERVICE_INSTANCE_ID": "g2-default-adapter",
    }
    spawn(workdir, "adapter-A", [py, "services/vehicle-agent-adapter/main.py"], env_a)
    spawn(
        workdir,
        "ingress-A",
        [py, "services/mqtt-ingress/main.py"],
        {
            **base,
            "FIREBOT_LEGACY_HEARTBEAT": "1",
            "FIREBOT_SERVICE_INSTANCE_ID": "g2-default-ingress",
        },
    )
    env_b = {
        **base,
        "MQTT_PORT": str(port_b),
        "MQTT_PASSWORD": "",
        "MQTT_PASSWORD_FILE": str(platform_b),
        "VEHICLE_AGENT_SCOPE_VEHICLE_IDS": VEHICLE,
        "VEHICLE_AGENT_TRANSPORT_PROFILE": PROFILE,
    }
    spawn(
        workdir,
        "adapter-B",
        [py, "services/vehicle-agent-adapter/main.py"],
        {**env_b, "FIREBOT_SERVICE_INSTANCE_ID": DEDICATED_ADAPTER},
    )
    spawn(
        workdir,
        "ingress-B",
        [py, "services/mqtt-ingress/main.py"],
        {**env_b, "FIREBOT_SERVICE_INSTANCE_ID": DEDICATED_INGRESS},
    )
    spawn(
        workdir,
        "dispatcher",
        [py, "services/command-dispatcher/main.py"],
        {
            **base,
            "FIREBOT_SERVICE_INSTANCE_ID": "g2-dispatcher",
            "DISPATCHER_BROKER_PROFILES_FILE": str(profiles),
        },
    )

    def ready():
        return all(
            redis.get(key)
            for key in (
                "service:vehicle-agent-adapter:heartbeat",
                f"service:vehicle-agent-adapter:heartbeat:{DEDICATED_ADAPTER}",
                f"service:mqtt-ingress:heartbeat:{DEDICATED_INGRESS}",
                "service:command-dispatcher:outbox-heartbeat",
                f"service:command-dispatcher:broker-heartbeat:{PROFILE}",
            )
        )

    assert wait_for(ready, 40), "services not healthy: " + "".join(
        f"\n--- {p.name}\n{p.read_text()[-1500:]}" for p in workdir.glob("*.log")
    )

    ros = rospy_python()
    ros_procs: list = []
    bridge_socket = workdir / "ros-bridge.sock"
    if ros:
        python3, ros_env = ros
        ros_port = free_port()
        log_conf = workdir / "ros_python_logging.conf"
        log_conf.write_text(
            "[loggers]\nkeys=root,rosout\n[handlers]\nkeys=stdout\n[formatters]\nkeys=plain\n"
            "[logger_root]\nlevel=INFO\nhandlers=stdout\n"
            "[logger_rosout]\nlevel=INFO\nhandlers=stdout\npropagate=0\nqualname=rosout\n"
            "[handler_stdout]\nclass=StreamHandler\nlevel=INFO\nformatter=plain\n"
            "args=(sys.stdout,)\n[formatter_plain]\nformat=%(asctime)s %(name)s %(message)s\n"
        )
        ros_env = {
            **ros_env,
            # 与 Noetic output=screen 等价：节点 [SOFTWARE_TEST] 日志落到进程 stdout 作证据
            "ROS_PYTHON_LOG_CONFIG_FILE": str(log_conf),
            "PYTHONUNBUFFERED": "1",
            "ROS_MASTER_URI": f"http://127.0.0.1:{ros_port}",
            "ROS_HOME": str(workdir / "roshome"),
            "ROS_LOG_DIR": str(workdir / "roslog"),
        }
        master = shutil.which("rosmaster") or str(Path(os.environ.get("G2_ROSMASTER", "rosmaster")))
        ros_procs.append(
            spawn(workdir, "rosmaster", [python3, master, "--core", "-p", str(ros_port)], ros_env)
        )
        assert wait_port(ros_port), "rosmaster did not listen"
        # 001 正式软件节点（integration/ros1/firebot-001-software/src/command_node.py）
        # 与其自定义消息（genpy 生成，等价 catkin generate_messages）——不使用模拟节点
        msg_root = generate_001_messages(python3, ros_env, workdir)
        ros_env = {
            **ros_env,
            "PYTHONPATH": os.pathsep.join([str(msg_root), ros_env["PYTHONPATH"]]),
        }
        ros_procs.append(
            spawn(
                workdir,
                "ros-001-command-node",
                [python3, str(FIREBOT_001_PKG / "src" / "command_node.py")],
                ros_env,
            )
        )
        ros_procs.append(
            spawn(
                workdir,
                "ros-bridge",
                [
                    python3,
                    "-m",
                    "services.vehicle_agent_v1_exec.ros.software_bridge",
                    "--namespace",
                    ROS_NAMESPACE,
                    "--socket",
                    str(bridge_socket),
                ],
                ros_env,
            )
        )
        assert wait_for(bridge_socket.exists, 20), "ROS bridge socket missing"
        time.sleep(2.0)  # 等待 ROS 话题连接建立

    sniff_a = Sniffer(
        port_a,
        PLATFORM_PASSWORD_A,
        certs / "ca.crt",
        ["v1/robot/+/cmd", "_platform/vehicle-agent-v1/+/command", "robot/+/command"],
    )
    sniff_b = Sniffer(
        port_b,
        PLATFORM_PASSWORD_B,
        certs / "ca.crt",
        [
            "v1/robot/+/cmd",
            "v1/robot/+/ack",
            "v1/robot/+/event",
            "_platform/vehicle-agent-v1/+/command",
            "robot/+/command",
        ],
    )
    context = {
        "workdir": workdir,
        "certs": certs,
        "port_a": port_a,
        "port_b": port_b,
        "redis": redis,
        "sniff_a": sniff_a,
        "sniff_b": sniff_b,
        "ros": bool(ros),
        "bridge_socket": bridge_socket,
        "agent": None,
        "evidence": {},
    }
    try:
        yield context
    finally:
        sniff_a.close()
        sniff_b.close()
        stop(context.get("agent"))
        for proc in reversed(_PROCS):
            stop(proc)
        for handle in _HANDLES:
            handle.close()
        # 共享测试库：恢复 001 资产原状并清除本套件命令，避免影响其他真实 E2E 套件
        _restore_vehicle_asset(original)
        try:
            from app.db.session import engine

            engine.dispose()
        except Exception:  # noqa: BLE001
            pass


ROBOT_FIELDS = (
    "agent_protocol",
    "enabled",
    "online_state",
    "boot_id",
    "agent_state_json",
    "estop_active",
)


def _snapshot_vehicle_asset() -> dict | None:
    from app.db.models import Robot, RobotCapability
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        if robot is None:
            return None
        capability = db.get(RobotCapability, robot.id)
        return {
            **{field: getattr(robot, field) for field in ROBOT_FIELDS},
            "capabilities": None if capability is None else capability.supported_commands_json,
        }


def _restore_vehicle_asset(original: dict | None) -> None:
    from app.db.models import Command, OutboxEvent, Robot, RobotCapability
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        if robot is None:
            return
        ids = db.scalars(select(Command.command_id).where(Command.robot_id == robot.id)).all()
        if ids:
            db.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(ids)))
            db.execute(delete(Command).where(Command.robot_id == robot.id))
        if original is None:
            # 本套件创建的资产：连同依赖行一并删除（仅测试库）
            from app.db.models import Base

            for table in reversed(Base.metadata.sorted_tables):
                if "robot_id" in table.c and table.name != "robots":
                    db.execute(table.delete().where(table.c.robot_id == robot.id))
            db.execute(delete(Robot).where(Robot.id == robot.id))
        else:
            for field in ROBOT_FIELDS:
                setattr(robot, field, original[field])
            capability = db.get(RobotCapability, robot.id)
            if capability is not None and original["capabilities"] is not None:
                capability.supported_commands_json = original["capabilities"]
        db.commit()


TEST_ADMIN = "g2-e2e-admin"
TEST_ADMIN_PASSWORD = "G2-E2E-Isolated-Only-2026!"


def ensure_isolated_test_admin() -> tuple[str, str]:
    """只在隔离测试库创建/重置专用测试管理员（不触碰 bootstrap admin，不猜密码）。"""
    from app.core.config import get_settings
    from app.core.security import hash_password, verify_password
    from app.db.models import Role, User, user_roles
    from app.db.session import SessionLocal
    from sqlalchemy import insert, select

    settings = get_settings()
    if settings.app_env != "test" or os.environ.get("REAL_E2E") != "1":
        raise RuntimeError("isolated test admin may only be created in APP_ENV=test REAL_E2E=1")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == TEST_ADMIN))
        if user is None:
            user = User(
                username=TEST_ADMIN,
                password_hash=hash_password(TEST_ADMIN_PASSWORD),
                display_name="G2 isolated E2E admin",
            )
            db.add(user)
            db.flush()
        elif not verify_password(TEST_ADMIN_PASSWORD, user.password_hash):
            user.password_hash = hash_password(TEST_ADMIN_PASSWORD)
        user.must_change_password = False
        user.failed_attempts = 0
        user.locked_until = None
        user.status = "ACTIVE"
        role = db.scalar(select(Role).where(Role.code == "super_admin"))
        if not db.execute(
            select(user_roles).where(
                user_roles.c.user_id == user.id, user_roles.c.role_id == role.id
            )
        ).first():
            db.execute(insert(user_roles).values(user_id=user.id, role_id=role.id))
        db.commit()
    return TEST_ADMIN, TEST_ADMIN_PASSWORD


def _prepare_vehicle_asset() -> None:
    """001 云端资产 = 生产同款基线：V1、enabled=false、capabilities=[]、estop NULL。"""
    from app.db.models import Robot, RobotCapability, RobotIntegrationProfile, Site
    from app.db.session import SessionLocal
    from sqlalchemy import null, select

    with SessionLocal() as db:
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        if robot is None:
            robot = Robot(vehicle_id=VEHICLE, name="firebot-001", site_id=site.id)
            db.add(robot)
            db.flush()
        # 测试库专用：清除上次运行遗留的 001 命令/Outbox（中断运行会留下待发命令）
        from app.db.models import Command, OutboxEvent
        from sqlalchemy import delete

        ids = db.scalars(select(Command.command_id).where(Command.robot_id == robot.id)).all()
        if ids:
            db.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(ids)))
            db.execute(delete(Command).where(Command.robot_id == robot.id))
        robot.agent_protocol = "VEHICLE_AGENT_V1"
        robot.enabled = False
        robot.online_state = "OFFLINE"
        robot.boot_id = None
        robot.agent_state_json = {}
        robot.estop_active = null()
        integration = db.get(RobotIntegrationProfile, robot.id) or RobotIntegrationProfile(
            robot_id=robot.id
        )
        integration.source_kind = "VEHICLE_AGENT_V1"
        integration.control_contract_verified = False
        integration.ack_contract_verified = False
        integration.map_contract_verified = False
        integration.stale_seconds = 8
        integration.offline_seconds = 15
        db.merge(integration)
        capability = db.get(RobotCapability, robot.id) or RobotCapability(robot_id=robot.id)
        capability.protocol_version = "vehicle-agent-v1"
        capability.supported_commands_json = []
        capability.sensors_json, capability.media_json = [], []
        db.merge(capability)
        db.commit()


def write_agent_files(g2: dict, *, profile: str = "ros-software-test") -> tuple[Path, Path, Path]:
    workdir: Path = g2["workdir"]
    identity = workdir / "device_identity.json"
    identity.write_text(
        json.dumps(
            {
                "vehicle_id": VEHICLE,
                "display_name": "firebot-001",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": g2["port_b"],
                "mqtt_username": VEHICLE,
                "mqtt_password": VEHICLE_PASSWORD,
                "ca_cert_path": str(g2["certs"] / "ca.crt"),
            }
        )
    )
    identity.chmod(0o600)
    grant = workdir / "execution_grant.json"
    grant.write_text(
        json.dumps(
            {
                "vehicle_id": VEHICLE,
                "execution_profile": profile,
                "ros_bridge_socket": str(g2["bridge_socket"]),
                "not_after": "2099-01-01T00:00:00+00:00",
            }
        )
    )
    grant.chmod(0o600)
    run_dir = workdir / "agent-run"
    return identity, grant, run_dir


def start_agent(g2: dict) -> subprocess.Popen:
    identity, grant, run_dir = write_agent_files(g2)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = spawn(
        g2["workdir"],
        f"vehicle-agent-exec-{int(time.time() * 1000)}",
        [
            sys.executable,
            "-m",
            "services.vehicle_agent_v1_exec.main",
            "--config",
            str(identity),
            "--grant",
            str(grant),
            "--run-dir",
            str(run_dir),
        ],
        env,
    )
    g2["agent"] = proc
    return proc
