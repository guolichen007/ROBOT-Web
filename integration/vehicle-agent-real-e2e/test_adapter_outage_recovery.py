"""G1-P2-L0 A1：云端 Adapter 中断后的在线恢复（真实组件反证，不重启 Agent）。

场景：
  1) 独立 Broker + 专属 Adapter/Ingress + 只读 Agent：车辆 ONLINE。
  2) 杀 Adapter（Agent 与 Broker 连接不断）：Ingress 清扫器 >15s 后写入
     availability=offline 并将车辆置 OFFLINE。
  3) 重启 Adapter（不重启 Agent）：Agent 周期性 live availability online（5s 非
     retained）应解除 offline 压制，车辆 ≤8s 恢复 ONLINE。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from app.db.models import Robot
from app.db.session import SessionLocal
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]
VEHICLE = "firebot-vehicle-01"
VEHICLE_PASSWORD = "e2e-vehicle-01-password"
INSTANCE = "r001-outage-recovery"


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


def _db_state() -> tuple[str | None, str | None]:
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        return robot.online_state, ((robot.agent_state_json or {}).get("availability"))


def _spawn_service(name: str, env: dict, workdir: Path) -> tuple[subprocess.Popen, object]:
    log = open(workdir / f"{name}.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, f"services/{name}/main.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def test_adapter_outage_recovers_without_agent_restart(e2e_stack, vehicle_asset):
    workdir: Path = e2e_stack["workdir"]
    port2 = _free_port()
    conf2 = workdir / "mosquitto.outage.conf"
    conf2.write_text(
        "\n".join(
            [
                f"listener {port2} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {workdir / 'passwd'}",
                f"acl_file {workdir / 'acl'}",
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
    broker2_log = open(workdir / "mosquitto.outage.log", "ab")  # noqa: SIM115
    broker2 = subprocess.Popen(
        ["mosquitto", "-c", str(conf2)],
        stdout=broker2_log,
        stderr=subprocess.STDOUT,
    )
    procs = [broker2]
    handles = [broker2_log]
    try:
        assert broker2.poll() is None, "outage broker exited immediately"
        assert _port_open("127.0.0.1", port2, 15.0), "outage broker did not listen"

        env = dict(os.environ)
        env.pop("FIREBOT_LEGACY_HEARTBEAT", None)
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
        env["MQTT_PORT"] = str(port2)
        env["MQTT_CA_FILE"] = str(e2e_stack["certs"] / "ca.crt")
        env["FIREBOT_SERVICE_INSTANCE_ID"] = INSTANCE
        env["VEHICLE_AGENT_SCOPE_VEHICLE_IDS"] = VEHICLE

        ingress, ingress_log = _spawn_service("mqtt-ingress", env, workdir)
        procs.append(ingress)
        handles.append(ingress_log)
        time.sleep(3)
        adapter, adapter_log = _spawn_service("vehicle-agent-adapter", env, workdir)
        procs.append(adapter)
        handles.append(adapter_log)

        config = workdir / "identity-outage.json"
        config.write_text(
            json.dumps(
                {
                    "vehicle_id": VEHICLE,
                    "display_name": "firebot-001",
                    "mqtt_host": "127.0.0.1",
                    "mqtt_port": port2,
                    "mqtt_username": VEHICLE,
                    "mqtt_password": VEHICLE_PASSWORD,
                    "ca_cert_path": str(e2e_stack["certs"] / "ca.crt"),
                }
            )
        )
        config.chmod(0o600)
        agent_log = open(workdir / "agent.outage.log", "ab")  # noqa: SIM115
        agent = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "services.vehicle_agent_v1_readonly.main",
                "--config",
                str(config),
                "--run-dir",
                str(workdir / "agent-outage-run"),
                "--status-interval",
                "1.5",
                "--connect-wait",
                "3",
            ],
            cwd=str(REPO_ROOT),
            env={k: v for k, v in os.environ.items() if k not in ("FIREBOT_VEHICLE_ID",)},
            stdout=agent_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(agent)
        handles.append(agent_log)

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and _db_state()[0] != "ONLINE":
            time.sleep(0.5)
        assert _db_state()[0] == "ONLINE", "初始 ONLINE 未达成"

        # 2) 杀 Adapter（Agent 与 Broker 连接保持）→ 清扫器 offline
        adapter.terminate()
        adapter.wait(timeout=5)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and _db_state()[0] != "OFFLINE":
            time.sleep(0.5)
        state, availability = _db_state()
        assert state == "OFFLINE", "Adapter 中断后清扫器未将车辆置 OFFLINE"
        assert availability == "offline", "清扫器未写入 availability=offline"

        # 3) 重启 Adapter（不重启 Agent）→ 周期性 live online 恢复 ONLINE
        adapter2, adapter2_log = _spawn_service("vehicle-agent-adapter", env, workdir)
        procs.append(adapter2)
        handles.append(adapter2_log)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _db_state()[0] != "ONLINE":
            time.sleep(0.5)
        state, availability = _db_state()
        assert state == "ONLINE", "Adapter 恢复后 ≤15s 未恢复 ONLINE（无 Agent 重启）"
        assert availability == "online", "availability 未恢复 online"
        assert agent.poll() is None, "测试期间 Agent 进程不得重启"
    finally:
        # 先停 Agent（Adapter/Ingress 仍在，优雅 offline 必须被中继落库），
        # 等待车辆 OFFLINE 后再停其余组件——保证测试结束为 OFFLINE 基线，
        # 不污染后续测试的 ONLINE 等待。
        if agent.poll() is None:
            agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait(timeout=5)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _db_state()[0] == "ONLINE":
            time.sleep(0.5)
        for proc in procs:
            if proc is agent or proc.poll() is not None:
                continue
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
