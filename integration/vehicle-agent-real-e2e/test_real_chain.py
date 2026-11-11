"""G1-R0 真实组件离线 E2E：真实 Mosquitto → Adapter → Ingress → DB/Redis → API。

真实 Agent 进程 + 真实 Mosquitto（TLS + per-device 凭证 + 生产同款 ACL）+
现有 Adapter/Ingress 进程 + 真实 PostgreSQL/Redis + 现有 FastAPI（TestClient）。
全部只连 127.0.0.1 测试端口，不连接生产 Broker，不复用 R001/R002 历史数据。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from conftest import (
    GHOST_VEHICLE,
    NOACL_VEHICLE,
    REPO_ROOT,
    VEHICLE,
    VEHICLE_PASSWORD,
    _port_open,
)

from services.vehicle_agent_v1 import topic

STATUS_TOPIC = topic(VEHICLE, "status")
AVAIL_TOPIC = topic(VEHICLE, "availability")


def wait_until(cond, timeout: float = 25.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {message}")


def _write_identity(
    workdir: Path,
    certs: Path,
    port: int,
    *,
    password: str = VEHICLE_PASSWORD,
    ca_name: str = "ca.crt",
    vehicle_id: str = VEHICLE,
) -> Path:
    config = workdir / f"identity-{vehicle_id}-{os.getpid()}.json"
    config.write_text(
        json.dumps(
            {
                "vehicle_id": vehicle_id,
                "display_name": "firebot-001",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": port,
                "mqtt_username": vehicle_id,
                "mqtt_password": password,
                "ca_cert_path": str(certs / ca_name),
            }
        )
    )
    config.chmod(0o600)
    return config


def _start_agent(config: Path, run_dir: Path, *, status_interval: str = "1.5") -> subprocess.Popen:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("FIREBOT_VEHICLE_ID", "BRIDGE_VEHICLE_ID")
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "services.vehicle_agent_v1_readonly.main",
            "--config",
            str(config),
            "--run-dir",
            str(run_dir),
            "--status-interval",
            status_interval,
            "--telemetry-interval",
            "2.5",
            "--connect-wait",
            "3",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_agent(proc: subprocess.Popen, *, sig: int = signal.SIGTERM) -> tuple[str, str]:
    proc.send_signal(sig)
    proc.wait(timeout=15)
    stdout, stderr = proc.communicate()
    return stdout, stderr


def _robot_state(auth_headers, api_client) -> dict:
    response = api_client.get(f"/api/v1/robots/{VEHICLE}", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()


def _robot_db_row():
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        return db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))


def test_real_agent_flow_reaches_api_online(e2e_stack, vehicle_asset, api_client, auth_headers):
    """1/2/3/4：真实 Agent 上报 → Adapter 规范化 → Ingress 落库 → API ONLINE + 真实时间戳。"""
    stack = e2e_stack
    config = _write_identity(stack["workdir"], stack["certs"], stack["mqtt_port"])
    proc = _start_agent(config, stack["workdir"] / "run-online")
    try:
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "ONLINE",
            30.0,
            "API ONLINE",
        )
        state = _robot_state(auth_headers, api_client)
        assert state["agent_protocol"] == "VEHICLE_AGENT_V1"
        last_seen = datetime.fromisoformat(state["last_seen_at"])
        assert 0 <= (datetime.now(UTC) - last_seen).total_seconds() <= 15.0  # 真实时间戳且新鲜
        agent_state = state["agent_state_json"]
        status = agent_state.get("status") or {}
        # 未接底盘：programs 只反映真实进程观测，绝不伪造就绪。
        assert status.get("programs", {}).get("agent") is True
        assert status.get("programs", {}).get("chassis") is False
        assert state["current_mode"] == "IDLE"
        assert state["battery"] is None  # 无假电池
        telemetry = agent_state.get("telemetry") or {}
        assert telemetry.get("pose") is None and telemetry.get("battery") is None  # 无假位姿
        latest = api_client.get(f"/api/v1/robots/{VEHICLE}/latest", headers=auth_headers)
        assert latest.status_code == 200 and latest.json()["online_state"] == "ONLINE"
    finally:
        _stop_agent(proc)


def test_graceful_sigterm_confirmed_offline(e2e_stack, vehicle_asset, api_client, auth_headers):
    """5：SIGTERM → retained offline 获 broker PUBACK → API 转 OFFLINE。"""
    stack = e2e_stack
    config = _write_identity(stack["workdir"], stack["certs"], stack["mqtt_port"])
    proc = _start_agent(config, stack["workdir"] / "run-sigterm")
    try:
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "ONLINE",
            30.0,
            "ONLINE before SIGTERM",
        )
        stdout, stderr = _stop_agent(proc)
        assert proc.returncode == 0
        assert "broker-confirmed" in stderr  # GRACEFUL_OFFLINE_CONFIRMED：日志只报确认送达
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "OFFLINE",
            20.0,
            "OFFLINE after SIGTERM",
        )
        state = _robot_state(auth_headers, api_client)
        assert (state["agent_state_json"] or {}).get("availability") == "offline"
        assert state["last_seen_at"] is None  # 离线即清空新鲜度
    finally:
        if proc.poll() is None:
            _stop_agent(proc)


def test_crash_lwt_offline_then_restart_online(e2e_stack, vehicle_asset, api_client, auth_headers):
    """5：SIGKILL 异常断线 → 真实 Mosquitto LWT → OFFLINE；重启 → ONLINE（boot 轮换）。"""
    stack = e2e_stack
    config = _write_identity(stack["workdir"], stack["certs"], stack["mqtt_port"])
    proc = _start_agent(config, stack["workdir"] / "run-crash")
    try:
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "ONLINE",
            30.0,
            "ONLINE before crash",
        )
        boot_before = _robot_db_row().boot_id
        _stop_agent(proc, sig=signal.SIGKILL)  # 异常断线（无 DISCONNECT）
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "OFFLINE",
            20.0,
            "LWT offline after crash",
        )
    finally:
        if proc.poll() is None:
            _stop_agent(proc)
    second = _start_agent(config, stack["workdir"] / "run-crash")
    try:
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "ONLINE",
            30.0,
            "ONLINE after restart",
        )
        assert _robot_db_row().boot_id != boot_before  # OFFLINE→ONLINE 新 boot
    finally:
        _stop_agent(second)


def test_wrong_credentials_rejected_stays_offline(
    e2e_stack, vehicle_asset, api_client, auth_headers
):
    """6：假凭证 → 真实 broker CONNACK 拒绝 → API 保持 OFFLINE，重试不上行。"""
    stack = e2e_stack
    config = _write_identity(
        stack["workdir"], stack["certs"], stack["mqtt_port"], password="wrong-password"
    )
    boot_before = _robot_db_row().boot_id
    proc = _start_agent(config, stack["workdir"] / "run-badcred")
    try:
        time.sleep(4.0)  # 留出多次连接尝试窗口
        assert _robot_state(auth_headers, api_client)["online_state"] == "OFFLINE"
        row = _robot_db_row()
        assert row.last_seen_at is None  # 假凭证从未建立在线会话
        assert row.boot_id == boot_before  # 无新 boot（拒绝即无会话轮换）
        stdout, stderr = _stop_agent(proc)
        assert "connect failed" in stderr  # 有界重试，未放弃（SIGTERM 正常退出 0）
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            _stop_agent(proc)


def test_wrong_ca_rejected_stays_offline(e2e_stack, vehicle_asset, api_client, auth_headers):
    """6：错误 CA → TLS 握手拒绝 → API 保持 OFFLINE，绝不降级 insecure。"""
    stack = e2e_stack
    config = _write_identity(
        stack["workdir"], stack["certs"], stack["mqtt_port"], ca_name="other_ca.crt"
    )
    proc = _start_agent(config, stack["workdir"] / "run-badca")
    try:
        time.sleep(4.0)
        assert _robot_state(auth_headers, api_client)["online_state"] == "OFFLINE"
        stdout, stderr = _stop_agent(proc)
        assert "connect failed" in stderr
    finally:
        if proc.poll() is None:
            _stop_agent(proc)


def test_cross_device_topic_denied_and_r002_untouched(e2e_stack, vehicle_asset, r002_snapshot):
    """6/9：跨设备 topic 被 broker ACL 拒绝；R002 全程零变更。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    stack = e2e_stack
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="e2e-cross-device-probe",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(
        ca_certs=str(stack["certs"] / "ca.crt"),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.username_pw_set(VEHICLE, VEHICLE_PASSWORD)
    client.connect("127.0.0.1", stack["mqtt_port"], keepalive=10)
    client.loop_start()
    try:
        wait_until(lambda: client.is_connected(), 10.0, "probe connected")
        # 用 firebot-vehicle-01 的凭证写 R002 的 topic：ACL pattern 必须拒绝。
        # 使用"若被转发将真实改变 R002 状态"的合法 status 载荷，
        # 使 R002 零变更成为 ACL 生效的强证明（而非载荷无效的弱证明）。
        hostile_status = json.dumps(
            {
                "online": True,
                "programs": {"agent": True},
                "task": {
                    "state": "idle",
                    "patrol_next": None,
                    "patrol_count": 0,
                    "patrol_paused": False,
                },
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        info = client.publish(topic("firebot-vehicle-02", "status"), hostile_status, qos=1)
        try:
            info.wait_for_publish(2.0)
        except RuntimeError:
            pass
        # 版本差异说明：mosquitto 1.6 对 ACL 拒绝的 QoS1 消息回 PUBACK 后静默丢弃；
        # 2.x 以 Not authorized 断开客户端。两者都保证消息不达订阅方——由下方
        # R002 零变更断言给出最终证明。
        time.sleep(2.0)
    finally:
        client.disconnect()
        client.loop_stop()
    with SessionLocal() as db:
        r002 = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        assert r002.online_state == r002_snapshot["online_state"]
        assert r002.agent_state_json == r002_snapshot["agent_state_json"]
        assert r002.boot_id == r002_snapshot["boot_id"]
        assert r002.updated_at == r002_snapshot["updated_at"]


def test_unknown_device_dropped_by_ingress(e2e_stack):
    """6：未知设备 ID（有凭证、无云端资产）→ ingress 静默丢弃、不崩溃、不建行。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    stack = e2e_stack
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{GHOST_VEHICLE}-readonly-v1",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(
        ca_certs=str(stack["certs"] / "ca.crt"),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.username_pw_set(GHOST_VEHICLE, stack["ghost_password"])
    client.connect("127.0.0.1", stack["mqtt_port"], keepalive=10)
    client.loop_start()
    try:
        wait_until(lambda: client.is_connected(), 10.0, "ghost connected")
        info = client.publish(
            topic(GHOST_VEHICLE, "status"),
            json.dumps(
                {
                    "online": True,
                    "programs": {"agent": True},
                    "task": {
                        "state": "idle",
                        "patrol_next": None,
                        "patrol_count": 0,
                        "patrol_paused": False,
                    },
                    "ts": datetime.now(UTC).isoformat(),
                }
            ),
            qos=1,
        )
        info.wait_for_publish(5.0)
        assert info.is_published()  # broker/ACL 层合法（写自己的 topic）
        time.sleep(2.0)  # 给 adapter→ingress 转发留时间
    finally:
        client.disconnect()
        client.loop_stop()
    with SessionLocal() as db:
        assert db.scalar(select(Robot).where(Robot.vehicle_id == GHOST_VEHICLE)) is None
    # ingress 仍健康：心跳新鲜，未因未知设备崩溃。
    heartbeat = stack["redis"].get("service:mqtt-ingress:heartbeat")
    assert heartbeat, "mqtt-ingress heartbeat missing"
    age = (datetime.now(UTC) - datetime.fromisoformat(heartbeat.decode())).total_seconds()
    assert age <= 30.0


def test_no_fake_hardware_and_control_gate(e2e_stack, vehicle_asset, api_client, auth_headers):
    """7/8：无假电池/假位姿/假就绪；Agent 不订阅 cmd；云端 downlink 关闭；无命令数据。"""
    from app.core.config import get_settings
    from app.db.models import Command, Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    stack = e2e_stack
    assert get_settings().vehicle_agent_v1_downlink_enabled is False  # CONTROL_DISABLED
    assert not get_settings().vehicle_downlink_allowlist_set  # 空 allowlist

    config = _write_identity(stack["workdir"], stack["certs"], stack["mqtt_port"])
    proc = _start_agent(config, stack["workdir"] / "run-gate")
    try:
        wait_until(
            lambda: _robot_state(auth_headers, api_client)["online_state"] == "ONLINE",
            30.0,
            "ONLINE for gate checks",
        )
        state = _robot_state(auth_headers, api_client)
        assert state["battery"] is None and state["estop_active"] is False
        assert state["current_mode"] == "IDLE"
        with SessionLocal() as db:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
            assert db.scalar(select(Command).where(Command.robot_id == robot.id)) is None
    finally:
        stdout, stderr = _stop_agent(proc)
    # 真实 Mosquitto 日志反证：Agent 客户端从未发起任何 SUBSCRIBE（含 cmd）。
    log = stack["mosquitto_log"].read_text(errors="replace")
    for line in log.splitlines():
        if "SUBSCRIBE" in line:
            assert f"{VEHICLE}-readonly-v1" not in line, f"agent subscribed: {line}"


def test_acl_user_without_section_denied_by_default(e2e_stack, vehicle_asset):
    """Mosquitto %u pattern 语义：pattern 规则仅作用于其所在 user 段；
    passwd 有账号但 ACL 无 user 段的认证用户默认全拒（deny-all），
    绝不落入其他 user 段的 pattern。用"会真实改变状态"的合法载荷验证被丢弃。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    stack = e2e_stack
    with SessionLocal() as db:
        before = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE)).online_state
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{NOACL_VEHICLE}-probe",
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(
        ca_certs=str(stack["certs"] / "ca.crt"),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.username_pw_set(NOACL_VEHICLE, stack["noacl_password"])
    client.connect("127.0.0.1", stack["mqtt_port"], keepalive=10)
    client.loop_start()
    try:
        wait_until(lambda: client.is_connected(), 10.0, "noacl user connected (auth ok)")
        hostile = json.dumps(
            {
                "online": True,
                "programs": {"agent": True},
                "task": {
                    "state": "idle",
                    "patrol_next": None,
                    "patrol_count": 0,
                    "patrol_paused": False,
                },
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        info = client.publish(topic(VEHICLE, "status"), hostile, qos=1)
        try:
            info.wait_for_publish(2.0)
        except RuntimeError:
            pass
        time.sleep(2.0)  # 若被转发将改变 VEHICLE 状态；断言保持原状=ACL 丢弃
    finally:
        client.disconnect()
        client.loop_stop()
    with SessionLocal() as db:
        after = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE)).online_state
    assert after == before


def test_ca_rotation_isolated_old_trust_rejected(e2e_stack):
    """CA 轮换隔离验证：新 CA 信任的客户端可连新 broker；只信旧 CA 的客户端被拒；
    旧 CA 链（R002 式既有信任）不受影响——轮换方案不替换全局 CA。"""
    import subprocess

    stack = e2e_stack
    workdir = stack["workdir"]
    new_ca_dir = workdir / "ca2"
    new_ca_dir.mkdir(exist_ok=True)
    # 新 CA + 新服务器证书（TEST ONLY）
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(new_ca_dir / "ca.key"),
            "-out",
            str(new_ca_dir / "ca.crt"),
            "-days",
            "3650",
            "-subj",
            "/CN=Firebot Rotation Test CA (TEST ONLY)",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(new_ca_dir / "server.key"),
            "-out",
            str(new_ca_dir / "server.csr"),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    (new_ca_dir / "san.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(new_ca_dir / "server.csr"),
            "-CA",
            str(new_ca_dir / "ca.crt"),
            "-CAkey",
            str(new_ca_dir / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(new_ca_dir / "server.crt"),
            "-days",
            "3650",
            "-extfile",
            str(new_ca_dir / "san.cnf"),
        ],
        check=True,
        capture_output=True,
    )
    # 第二 broker（新 CA 签名），端口随机
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port2 = probe.getsockname()[1]
    conf2 = workdir / "mosquitto.ca2.conf"
    conf2.write_text(
        "\n".join(
            [
                f"listener {port2} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {workdir / 'passwd'}",
                f"cafile {new_ca_dir / 'ca.crt'}",
                f"certfile {new_ca_dir / 'server.crt'}",
                f"keyfile {new_ca_dir / 'server.key'}",
                "require_certificate false",
                "persistence false",
            ]
        )
        + "\n"
    )
    broker2 = subprocess.Popen(
        ["mosquitto", "-c", str(conf2)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1)
        assert _port_open("127.0.0.1", port2, 10.0), "rotation broker did not start"

        def try_connect(ca: Path, port: int) -> bool:
            probe_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id="rotation-probe",
                protocol=mqtt.MQTTv311,
            )
            probe_client.tls_set(
                ca_certs=str(ca),
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            probe_client.username_pw_set(VEHICLE, stack["vehicle_password"])
            try:
                probe_client.connect("127.0.0.1", port, keepalive=5)
            except (OSError, ssl.SSLError):
                return False  # TLS 验证失败即拒绝（绝不降级）
            probe_client.loop_start()
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not probe_client.is_connected():
                    time.sleep(0.1)
                connected = probe_client.is_connected()
            finally:
                probe_client.disconnect()
                probe_client.loop_stop()
            return connected

        # 新 CA 信任 → 新 broker 连接成功
        assert try_connect(new_ca_dir / "ca.crt", port2) is True
        # 旧 CA 信任 → 新 broker 被拒（旧信任路径关闭，不静默降级）
        assert try_connect(stack["certs"] / "ca.crt", port2) is False
        # 旧 CA 信任 → 旧 broker 依旧可用（R002 式既有信任链不受影响）
        assert try_connect(stack["certs"] / "ca.crt", stack["mqtt_port"]) is True
    finally:
        broker2.terminate()
        try:
            broker2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            broker2.kill()
            broker2.wait(timeout=10)
