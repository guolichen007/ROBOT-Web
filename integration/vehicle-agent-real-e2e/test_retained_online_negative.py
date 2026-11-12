"""G1-P2-L0 A：旧 retained ONLINE 反证（真实 Broker 重订阅，非构造 DB 记录）。

场景：
  1) 持久化 Broker2（autosave_interval=2s）承载 Agent，retained availability=online
     落盘；
  2) SIGKILL Broker2（无 LWT、无最终保存）后杀死 Agent——真实设备已离线，
     LWT offline 未送达；
  3) 重启 Broker2：磁盘恢复旧 retained online；
  4) 新会话专属 Adapter（新实例 ID → 全新订阅）重订阅，实证收到旧 retained online；
  5) 断言：DB 不得复活 ONLINE、last_seen_at 不得刷新、Redis latest 的
     server_received_at 不得刷新（Web 链路新鲜度不得被旧数据伪装）。
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
DEDICATED_INSTANCE = "r001-retained-negative"


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
        return robot.online_state, robot.last_seen_at.isoformat() if robot.last_seen_at else None


def test_old_retained_online_not_resurrected(e2e_stack, vehicle_asset):
    workdir: Path = e2e_stack["workdir"]
    redis = e2e_stack["redis"]
    # 干净基线：清掉本车 latest（避免跨测试/跨运行的 Redis 残留污染断言）
    redis.delete(f"robot:{VEHICLE}:latest")
    # 若上一测试遗留 ONLINE，等待清扫器退化为 OFFLINE（真实基线）
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and _db_state()[0] == "ONLINE":
        time.sleep(0.5)
    before_state, before_seen = _db_state()
    assert before_state != "ONLINE", "前置条件：车辆必须处于 OFFLINE 基线"
    port2 = _free_port()
    conf2 = workdir / "mosquitto.retained.conf"
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
                "persistence true",
                f"persistence_location {workdir / 'retained-data'}/",
                "autosave_interval 2",
                "log_dest stdout",
            ]
        )
        + "\n"
    )
    (workdir / "retained-data").mkdir(exist_ok=True)
    broker2_log = open(workdir / "mosquitto.retained.log", "ab")  # noqa: SIM115
    broker2 = subprocess.Popen(
        ["mosquitto", "-c", str(conf2)],
        stdout=broker2_log,
        stderr=subprocess.STDOUT,
    )
    procs = [broker2]
    try:
        assert broker2.poll() is None, "retained broker exited immediately"
        assert _port_open("127.0.0.1", port2, 15.0), "retained broker did not listen"

        # 1) Agent 在 Broker2 上线（retained online 落盘，autosave 2s）
        config = workdir / "identity-retained.json"
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
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        agent_log = open(workdir / "agent.retained.log", "ab")  # noqa: SIM115
        agent = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "services.vehicle_agent_v1_readonly.main",
                "--config",
                str(config),
                "--run-dir",
                str(workdir / "agent-retained-run"),
                "--status-interval",
                "1.5",
                "--connect-wait",
                "3",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=agent_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(agent)
        time.sleep(4)  # online retained + autosave 落盘

        # 2) SIGKILL Broker2（无 LWT、无最终保存）→ 杀 Agent
        broker2.kill()
        broker2.wait(timeout=5)
        agent.kill()
        agent.wait(timeout=5)

        # 3) 重启 Broker2：磁盘恢复旧 retained online
        broker2 = subprocess.Popen(
            ["mosquitto", "-c", str(conf2)],
            stdout=broker2_log,
            stderr=subprocess.STDOUT,
        )
        procs[0] = broker2
        assert _port_open("127.0.0.1", port2, 15.0), "retained broker did not restart"

        # 证明旧 retained online 确实存在且可被重订阅者收到
        probe = subprocess.run(
            [
                "mosquitto_sub",
                "-h",
                "127.0.0.1",
                "-p",
                str(port2),
                "--cafile",
                str(e2e_stack["certs"] / "ca.crt"),
                "-u",
                "platform",
                "-P",
                e2e_stack["platform_password"],
                "-t",
                f"v1/robot/{VEHICLE}/availability",
                "-C",
                "1",
                "-W",
                "6",
                "-v",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "online" in probe.stdout, f"旧 retained online 未恢复或不可投递: {probe.stdout}"

        # 4) 专属 Ingress 先订阅内部桥，再启动新会话 Adapter——消除订阅时序竞态，
        #    保证 retained 内部信封必然到达 ingress，由 G1-P2-L0 守卫丢弃。
        env2 = dict(os.environ)
        env2.pop("FIREBOT_LEGACY_HEARTBEAT", None)
        env2["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "apps" / "api")])
        env2["MQTT_PORT"] = str(port2)
        env2["MQTT_CA_FILE"] = str(e2e_stack["certs"] / "ca.crt")
        env2["FIREBOT_SERVICE_INSTANCE_ID"] = DEDICATED_INSTANCE
        env2["VEHICLE_AGENT_SCOPE_VEHICLE_IDS"] = VEHICLE
        ingress2_log = open(workdir / "ingress.retained.log", "ab")  # noqa: SIM115
        ingress2 = subprocess.Popen(
            [sys.executable, "services/mqtt-ingress/main.py"],
            cwd=str(REPO_ROOT),
            env=env2,
            stdout=ingress2_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(ingress2)
        time.sleep(4)  # ingress 完成订阅
        adapter2_log = open(workdir / "adapter.retained.log", "ab")  # noqa: SIM115
        adapter2 = subprocess.Popen(
            [sys.executable, "services/vehicle-agent-adapter/main.py"],
            cwd=str(REPO_ROOT),
            env=env2,
            stdout=adapter2_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(adapter2)
        time.sleep(6)

        # 5) 断言：守卫丢弃日志存在 + 不复活、不刷新 last_seen、不刷新 server_received_at
        drop_log = (workdir / "ingress.retained.log").read_text()
        assert "retained online dropped" in drop_log, (
            "专属 Ingress 未收到/未丢弃 retained online（订阅时序或守卫缺失）"
        )
        after_state, after_seen = _db_state()
        assert after_state != "ONLINE", "旧 retained online 复活了 OFFLINE 设备"
        assert after_seen == before_seen, "旧 retained 数据刷新了 last_seen_at"
        raw = redis.get(f"robot:{VEHICLE}:latest")
        latest = json.loads(raw) if raw else {}
        assert not latest.get("server_received_at"), "旧 retained 数据刷新了 Web server_received_at"
        # 真实 LWT offline 逻辑不受影响：Agent 已死但无 LWT 场景下，最终由清扫器/权威
        # offline 事件接管；此处只反证 retained online 不能成为 ONLINE 事实源。
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        broker2_log.close()
