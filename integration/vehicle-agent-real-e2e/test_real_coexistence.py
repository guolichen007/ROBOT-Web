"""G1-P2 真实双实例共存测试（原服务 + 专属服务共享同一 PG/Redis）。

拓扑：
  独立 Broker2（127.0.0.1 随机端口，同 ACL/凭证集）→ 专属 Adapter/Ingress
  （FIREBOT_SERVICE_INSTANCE_ID=r001-dedicated、VEHICLE_AGENT_SCOPE_VEHICLE_IDS=
  firebot-vehicle-01）→ 与 e2e_stack 的原 Broker/Adapter/Ingress 共享 PG/Redis。

断言：
- 双实例心跳键并存且互不覆盖（原实例键、专属实例键同时新鲜）。
- R001 Agent 走专属 Broker 正常 ONLINE 落库（经专属实例）。
- 范围白名单：专属 Broker 上 ghost 车辆的 v1 消息被专属实例丢弃，不落库。
- 原实例心跳持续更新（未被专属实例影响）。
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

REPO_ROOT = Path(__file__).resolve().parents[2]
VEHICLE = "firebot-vehicle-01"
VEHICLE_PASSWORD = "e2e-vehicle-01-password"
DEDICATED_INSTANCE = "r001-dedicated"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_open(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1.0)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _write_identity(workdir: Path, certs: Path, port: int) -> Path:
    config = workdir / "identity-dedicated.json"
    config.write_text(
        json.dumps(
            {
                "vehicle_id": VEHICLE,
                "display_name": "firebot-001",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": port,
                "mqtt_username": VEHICLE,
                "mqtt_password": VEHICLE_PASSWORD,
                "ca_cert_path": str(certs / "ca.crt"),
            }
        )
    )
    return config


SCOPE_GHOST = "firebot-vehicle-77"  # 有云端资产、凭证、ACL 段：只有范围过滤能拦下


def _ensure_scope_ghost_row() -> None:
    from app.db.models import Robot, Site
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SCOPE_GHOST))
        if robot is None:
            db.add(
                Robot(
                    vehicle_id=SCOPE_GHOST,
                    name="scope-ghost",
                    site_id=site.id,
                    agent_protocol="VEHICLE_AGENT_V1",
                    enabled=False,
                )
            )
            db.commit()


def test_dual_instance_coexistence(e2e_stack, vehicle_asset):
    _ensure_scope_ghost_row()
    workdir: Path = e2e_stack["workdir"]
    redis = e2e_stack["redis"]

    port2 = _free_port()
    acl2 = workdir / "acl.broker2"
    acl2.write_text(
        (workdir / "acl").read_text()
        + f"\nuser {SCOPE_GHOST}\n"
        + "pattern read v1/robot/%u/cmd\n"
        + "pattern write v1/robot/%u/status\n"
        + "pattern write v1/robot/%u/telemetry\n"
        + "pattern write v1/robot/%u/availability\n"
    )
    passwd2 = workdir / "passwd.broker2"

    shutil.copy(workdir / "passwd", passwd2)
    subprocess.run(
        ["mosquitto_passwd", "-b", str(passwd2), SCOPE_GHOST, "e2e-scope-ghost-password"],
        check=True,
        capture_output=True,
    )
    conf2 = workdir / "mosquitto.broker2.conf"
    conf2.write_text(
        "\n".join(
            [
                f"listener {port2} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {passwd2}",
                f"acl_file {acl2}",
                f"cafile {workdir / 'certs' / 'ca.crt'}",
                f"certfile {workdir / 'certs' / 'server.crt'}",
                f"keyfile {workdir / 'certs' / 'server.key'}",
                "require_certificate false",
                "persistence false",
                "log_dest stdout",
            ]
        )
        + "\n"
    )
    broker2_log = open(workdir / "mosquitto.broker2.log", "ab")  # noqa: SIM115
    broker2 = subprocess.Popen(
        ["mosquitto", "-c", str(conf2)],
        stdout=broker2_log,
        stderr=subprocess.STDOUT,
    )
    procs = [broker2]
    handles = [broker2_log]
    try:
        assert broker2.poll() is None, "broker2 exited immediately"
        assert _port_open("127.0.0.1", port2, 15.0), "broker2 did not listen"

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
        env["MQTT_PORT"] = str(port2)
        env["MQTT_CA_FILE"] = str(e2e_stack["certs"] / "ca.crt")
        env["FIREBOT_SERVICE_INSTANCE_ID"] = DEDICATED_INSTANCE
        env["VEHICLE_AGENT_SCOPE_VEHICLE_IDS"] = VEHICLE

        adapter2_log = open(workdir / "adapter.dedicated.log", "ab")  # noqa: SIM115
        handles.append(adapter2_log)
        adapter2 = subprocess.Popen(
            [sys.executable, "services/vehicle-agent-adapter/main.py"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=adapter2_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(adapter2)
        ingress2_log = open(workdir / "ingress.dedicated.log", "ab")  # noqa: SIM115
        handles.append(ingress2_log)
        ingress2 = subprocess.Popen(
            [sys.executable, "services/mqtt-ingress/main.py"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=ingress2_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(ingress2)

        def keys_ready() -> bool:
            return bool(
                redis.get(f"service:vehicle-agent-adapter:heartbeat:{DEDICATED_INSTANCE}")
                and redis.get(f"service:mqtt-ingress:heartbeat:{DEDICATED_INSTANCE}")
            )

        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline and not keys_ready():
            time.sleep(0.5)
        assert keys_ready(), (
            f"dedicated heartbeat keys missing\n"
            f"adapter2:\n{(workdir / 'adapter.dedicated.log').read_text()[-1500:]}"
        )

        # 原实例（hostname 实例键）与专属实例键并存
        original_keys = [
            key.decode()
            for key in redis.scan_iter(match="service:*:heartbeat:*")
            if key.decode() != f"service:vehicle-agent-adapter:heartbeat:{DEDICATED_INSTANCE}"
            and key.decode() != f"service:mqtt-ingress:heartbeat:{DEDICATED_INSTANCE}"
        ]
        assert original_keys, "原实例命名空间心跳键缺失（双写失效）"

        # R001 Agent 经 Broker2 上行（专属实例落库 ONLINE）
        agent_config = _write_identity(workdir, e2e_stack["certs"], port2)
        agent_log = open(workdir / "agent.dedicated.log", "ab")  # noqa: SIM115
        handles.append(agent_log)
        agent = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "services.vehicle_agent_v1_readonly.main",
                "--config",
                str(agent_config),
                "--run-dir",
                str(workdir / "agent-dedicated-run"),
                "--status-interval",
                "1.5",
                "--telemetry-interval",
                "2.5",
                "--connect-wait",
                "3",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=agent_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(agent)
        try:
            deadline = time.monotonic() + 40.0
            online = False
            while time.monotonic() < deadline:
                raw = redis.get(f"robot:{VEHICLE}:latest")
                if raw and json.loads(raw).get("online_state") == "ONLINE":
                    online = True
                    break
                time.sleep(0.5)
            assert online, (
                f"R001 未经专属链路 ONLINE\nagent:\n"
                f"{(workdir / 'agent.dedicated.log').read_text()[-1500:]}"
            )

            # 范围白名单：ghost 车辆消息在 Broker2 上被专属实例丢弃
            ghost_pub = subprocess.run(
                [
                    "mosquitto_pub",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(port2),
                    "--cafile",
                    str(e2e_stack["certs"] / "ca.crt"),
                    "-u",
                    SCOPE_GHOST,
                    "-P",
                    "e2e-scope-ghost-password",
                    "-t",
                    f"v1/robot/{SCOPE_GHOST}/status",
                    "-m",
                    '{"online":true,"programs":{},"task":{"state":"idle"},"ts":"2026-01-01T00:00:00+00:00"}',
                    "-q",
                    "1",
                ],
                capture_output=True,
                text=True,
            )
            assert ghost_pub.returncode == 0, ghost_pub.stderr
            time.sleep(3)
            assert not redis.exists(f"robot:{SCOPE_GHOST}:latest"), (
                "scope 白名单失效：范围外车辆消息被专属实例落库"
            )

            # 原实例兼容心跳仍在更新（专属实例未破坏原链路）
            stamp_before = redis.get("service:mqtt-ingress:heartbeat")
            time.sleep(3)
            stamp_after = redis.get("service:mqtt-ingress:heartbeat")
            assert stamp_before and stamp_after and stamp_before != stamp_after, (
                "原实例兼容心跳未持续更新"
            )
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=5)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for handle in handles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
