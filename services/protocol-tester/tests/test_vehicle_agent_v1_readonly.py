"""G0-A2 只读 Agent 本地隔离验收测试。

全部使用本地进程内 FakeBroker（TLS / CONNACK / LWT / retained / QoS 全实现），
禁止连接任何真实生产 Broker。测试证书为 TEST ONLY（certs/，可审计重建）。
"""

from __future__ import annotations

import json
import os
import random
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.vehicle_agent_v1 import Status, Telemetry, normalize, topic
from services.vehicle_agent_v1_readonly import AGENT_VERSION
from services.vehicle_agent_v1_readonly.agent import (
    AgentConfig,
    ReadOnlyAgent,
    build_status,
    build_telemetry,
    validate_interval,
)
from services.vehicle_agent_v1_readonly.collector import (
    collect,
    os_info,
    uptime_seconds,
)
from services.vehicle_agent_v1_readonly.guard import (
    OwnerConflictError,
    OwnerGuard,
    legacy_bridge_running,
)
from services.vehicle_agent_v1_readonly.identity import (
    IdentityError,
    load_identity,
)
from services.vehicle_agent_v1_readonly.main import EXIT_OWNER_CONFLICT
from services.vehicle_agent_v1_readonly.main import main as agent_main
from services.vehicle_agent_v1_readonly.transport import (
    MqttTransport,
    SubscribeForbiddenError,
    TransportConfig,
)

CERTS = Path(__file__).parent / "certs"
REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATE_CERTS_SCRIPT = REPO_ROOT / "scripts" / "generate_readonly_test_certs.sh"
VEHICLE = "firebot-vehicle-01"
TEST_PASSWORD = "local-acceptance-cred-CHANGE-ME"


@pytest.fixture(scope="session", autouse=True)
def _test_certs():
    """TEST-ONLY TLS 证书：仓库 .gitignore 排除 *.key/*.crt，测试会话开始时现场生成。"""
    if not (CERTS / "ca.crt").is_file():
        CERTS.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["bash", str(GENERATE_CERTS_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert completed.returncode == 0, f"test cert generation failed: {completed.stderr}"
    yield


def wait_until(cond: Callable[[], bool], timeout: float = 20.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


# ---------------------------------------------------------------------------
# 进程内最小 MQTTv311 FakeBroker（仅测试用）
# ---------------------------------------------------------------------------


class FakeBroker:
    """最小 MQTTv311 broker：TLS 服务端证书、CONNACK、LWT、retained、QoS1 PUBACK。"""

    def __init__(
        self,
        certfile: Path,
        keyfile: Path,
        *,
        expected_username: str,
        expected_password: str,
        ack_publishes: bool = True,
    ) -> None:
        self.expected_username = expected_username
        self.expected_password = expected_password
        self.ack_publishes = ack_publishes  # False = 只收不发 PUBACK（验证未确认语义）
        self.messages: list[dict] = []
        self.retained: dict[str, bytes] = {}
        self.subscriptions: list[str] = []
        self.connect_attempts: list[dict] = []
        self.accepted_sessions = 0
        self.lwt_emissions: list[tuple[str, str]] = []
        self.handshake_failures = 0
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(certfile), str(keyfile))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._sockets: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        for sock in list(self._sockets):
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._listener.close()
        except OSError:
            pass

    def abort_clients(self) -> None:
        """模拟网络中断：直接关闭全部客户端连接（触发客户端重连与 broker LWT）。"""
        for sock in list(self._sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- broker 内部 -----------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                raw, _ = self._listener.accept()
            except OSError:
                break
            try:
                tls = self._ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                with self._lock:
                    self.handshake_failures += 1
                try:
                    raw.close()
                except OSError:
                    pass
                continue
            # wrap_socket 后 raw 已 detach，必须跟踪持有 fd 的 TLS socket。
            with self._lock:
                self._sockets.append(tls)
            threading.Thread(target=self._serve, args=(tls,), daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        clean = True
        will: tuple[str, str] | None = None
        try:
            header, body = _read_packet(sock)
            if header != 0x10:
                return
            connect = _parse_connect(body)
            with self._lock:
                self.connect_attempts.append(
                    {
                        "client_id": connect["client_id"],
                        "username": connect["username"],
                        "password": connect["password"],
                    }
                )
            if connect["will"]:
                will = (connect["will"][0], connect["will"][1].decode("utf-8", errors="replace"))
            authorized = (
                connect["username"] == self.expected_username
                and connect["password"] == self.expected_password
            )
            sock.sendall(b"\x20\x02\x00\x00" if authorized else b"\x20\x02\x00\x05")
            if not authorized:
                return  # 凭证错误：CONNACK rc=5 后关闭，不产生 LWT
            with self._lock:
                self.accepted_sessions += 1
            while True:
                packet = _read_packet(sock)
                if packet is None:
                    break  # 连接在未收到 DISCONNECT 时关闭 → 异常断线
                header, body = packet
                if header == 0xC0:  # PINGREQ
                    sock.sendall(b"\xd0\x00")
                elif header == 0xE0:  # DISCONNECT：正常退出，不触发 LWT
                    return
                elif header & 0xF0 == 0x30:  # PUBLISH
                    self._handle_publish(sock, header, body, connect["client_id"])
                elif header & 0xF0 == 0x80:  # SUBSCRIBE
                    packet_id = int.from_bytes(body[:2], "big")
                    topic_len = int.from_bytes(body[2:4], "big")
                    name = body[4 : 4 + topic_len].decode()
                    with self._lock:
                        self.subscriptions.append(name)
                    sock.sendall(b"\x90\x03" + bytes([packet_id >> 8, packet_id & 0xFF, 0x00]))
            clean = False  # while 因 EOF 退出：异常断线
        except (OSError, EOFError, ValueError):
            clean = False
        finally:
            if not clean and will is not None:
                with self._lock:
                    self.lwt_emissions.append(will)
            try:
                sock.close()
            except OSError:
                pass

    def _handle_publish(
        self, sock: socket.socket, header: int, body: bytes, client_id: str
    ) -> None:
        qos = (header >> 1) & 0x03
        retain = bool(header & 0x01)
        offset = 0
        topic_len = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2
        name = body[offset : offset + topic_len].decode()
        offset += topic_len
        packet_id: int | None = None
        if qos > 0:
            packet_id = int.from_bytes(body[offset : offset + 2], "big")
            offset += 2
        payload = body[offset:]
        with self._lock:
            self.messages.append(
                {
                    "topic": name,
                    "payload": payload,
                    "qos": qos,
                    "retain": retain,
                    "client_id": client_id,
                }
            )
            if retain:
                self.retained[name] = payload
        if qos == 1 and packet_id is not None and self.ack_publishes:
            sock.sendall(b"\x40\x02" + bytes([packet_id >> 8, packet_id & 0xFF]))


def _read_packet(sock: socket.socket) -> tuple[int, bytes] | None:
    first = sock.recv(1)
    if not first:
        return None
    header = first[0]
    length = 0
    multiplier = 1
    while True:
        chunk = sock.recv(1)
        if not chunk:
            return None
        value = chunk[0]
        length += (value & 0x7F) * multiplier
        if not value & 0x80:
            break
        multiplier *= 128
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return header, body


def _parse_connect(body: bytes) -> dict:
    offset = 0

    def field() -> bytes:
        nonlocal offset
        size = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2
        value = body[offset : offset + size]
        offset += size
        return value

    field()  # protocol name "MQTT"
    level = body[offset]
    offset += 1
    assert level == 4, "FakeBroker expects MQTTv311"
    flags = body[offset]
    offset += 1
    offset += 2  # keepalive
    client_id = field().decode()
    will: tuple[str, bytes] | None = None
    if flags & 0x04:
        will_topic = field()
        will_payload = field()
        will = (will_topic.decode(), will_payload)
    username = field().decode() if flags & 0x80 else None
    password = field().decode() if flags & 0x40 else None
    return {"client_id": client_id, "will": will, "username": username, "password": password}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def identity_config(tmp_path: Path):
    def _write(
        port: int = 8883,
        *,
        password: str = TEST_PASSWORD,
        ca_name: str = "ca.crt",
        vehicle_id: str = VEHICLE,
        username: str | None = None,
    ) -> Path:
        config = tmp_path / f"device-{vehicle_id}.json"
        config.write_text(
            json.dumps(
                {
                    "vehicle_id": vehicle_id,
                    "display_name": "firebot-001",
                    "mqtt_host": "127.0.0.1",
                    "mqtt_port": port,
                    "mqtt_username": username or vehicle_id,
                    "mqtt_password": password,
                    "ca_cert_path": str(CERTS / ca_name),
                }
            )
        )
        config.chmod(0o600)  # G1-R0 A3：身份凭证文件必须 0600（权限受检）
        return config

    return _write


@pytest.fixture
def broker():
    brokers = []

    def _make(
        certfile: str = "server.crt",
        keyfile: str = "server.key",
        *,
        expected_username: str = VEHICLE,
        expected_password: str = TEST_PASSWORD,
        ack_publishes: bool = True,
    ) -> FakeBroker:
        instance = FakeBroker(
            CERTS / certfile,
            CERTS / keyfile,
            expected_username=expected_username,
            expected_password=expected_password,
            ack_publishes=ack_publishes,
        )
        brokers.append(instance)
        return instance

    yield _make
    for instance in brokers:
        instance.stop()


# ---------------------------------------------------------------------------
# A. 真实只读采集
# ---------------------------------------------------------------------------


def test_collector_reads_real_host_fields():
    snapshot = collect(
        AGENT_VERSION,
        {"never_running": ("__definitely_not_a_real_process_xyz__",), "pytest": ("pytest",)},
        now=datetime.now(UTC),
    )
    assert snapshot.agent_pid == os.getpid()
    assert snapshot.agent_version == AGENT_VERSION
    assert snapshot.os["kernel"]
    assert snapshot.os["name"]
    assert snapshot.uptime_seconds > 0
    assert len(snapshot.load) == 3 and all(isinstance(v, float) for v in snapshot.load)
    assert snapshot.memory["total"] > 0
    assert snapshot.disk["total"] > 0
    datetime.fromisoformat(snapshot.collected_at)
    # 真实进程存活检测：测试进程自身 cmdline 含 pytest；虚构进程名绝不出现。
    assert snapshot.programs["pytest"] is True
    assert snapshot.programs["never_running"] is False


def test_collector_no_fabrication_with_missing_sources(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    markers = {"chassis": ("firebot_chassis",), "navigation": ("nav2",), "smog": ("firebot_smog",)}
    # 没有任何进程目录：全部 False，不抛异常，不生成模拟值。
    snapshot = collect(AGENT_VERSION, markers, now=datetime.now(UTC), proc_root=proc_root)
    assert snapshot.programs == {"chassis": False, "navigation": False, "smog": False}

    # 出现真实底盘进程 cmdline 时：仅该项为 True。
    pid_dir = proc_root / "1234"
    pid_dir.mkdir()
    pid_dir.joinpath("cmdline").write_bytes(b"/usr/bin/firebot_chassis\x00--device\x00can0")
    snapshot = collect(AGENT_VERSION, markers, now=datetime.now(UTC), proc_root=proc_root)
    assert snapshot.programs == {"chassis": True, "navigation": False, "smog": False}


def test_collector_injected_os_files(tmp_path: Path):
    fake_uptime = tmp_path / "uptime"
    fake_uptime.write_text("12345.67 890.12\n")
    assert uptime_seconds(fake_uptime) == pytest.approx(12345.67)
    fake_release = tmp_path / "os-release"
    fake_release.write_text('NAME="Ubuntu"\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04"\n')
    info = os_info(fake_release)
    assert info["name"] == "Ubuntu" and info["version_id"] == "22.04"


# ---------------------------------------------------------------------------
# B. 上行合同与 Schema 兼容
# ---------------------------------------------------------------------------


def test_status_payload_validates_against_contract():
    payload = build_status({"chassis": False, "navigation": False}, "2026-11-11T05:00:00+00:00")
    status = Status.model_validate(payload)
    assert status.online is True
    assert status.programs["agent"] is True
    assert status.programs["chassis"] is False
    assert status.task.state == "idle"
    assert normalize("status", payload)["online"] is True


def test_status_semantics_no_chassis_ready_inference():
    # G1-R0 C：task=idle 与进程名匹配绝不推断底盘就绪；
    # 无进程观测时，机器人程序键恒为 false（观测语义，非就绪语义）。
    payload = build_status({}, "2026-11-11T05:00:00+00:00")
    status = Status.model_validate(payload)
    assert status.task.state == "idle"
    for name in ("chassis", "navigation", "patrol", "thermal", "battery", "smog", "camera"):
        assert status.programs[name] is False, f"{name} must not imply readiness"
    assert status.programs["agent"] is True


def test_telemetry_null_semantics_for_unconnected_hardware():
    payload = build_telemetry("2026-11-11T05:00:00+00:00")
    telemetry = Telemetry.model_validate(payload)
    assert telemetry.pose is None
    assert telemetry.battery is None
    assert telemetry.smog is None
    assert telemetry.thermal is None
    assert normalize("telemetry", payload)["pose"] is None


def test_unknown_extra_fields_ignored_by_platform():
    # 实验性扩展（默认关闭）进入 status 时，平台 UplinkModel extra=ignore 保证兼容。
    payload = build_status({}, "2026-11-11T05:00:00+00:00", host={"uptime_seconds": 1.0})
    normalized = normalize("status", payload)
    assert "host" not in normalized  # 平台语义：未知字段不进入业务状态


def test_availability_contract_forms():
    assert normalize("availability", "online") == {"state": "online"}
    assert normalize("availability", "offline") == {"state": "offline"}
    assert normalize("availability", {"state": "offline"}) == {"state": "offline"}


def test_topic_contract():
    assert topic(VEHICLE, "availability") == f"v1/robot/{VEHICLE}/availability"
    with pytest.raises(ValueError):
        topic(VEHICLE, "cmd_vel")  # 不存在的上行类型
    with pytest.raises(ValueError):
        topic("bad id!", "status")


# ---------------------------------------------------------------------------
# C. 身份与安全
# ---------------------------------------------------------------------------


def test_identity_single_source(identity_config, tmp_path: Path):
    config = identity_config()
    identity = load_identity(config, allow_placeholder_password=True)
    assert identity.vehicle_id == VEHICLE
    assert identity.mqtt_username == VEHICLE
    assert identity.client_id == f"{VEHICLE}-readonly-v1"


def test_identity_forbidden_and_invalid(identity_config, tmp_path: Path):
    for forbidden in ("R001", "R002", "firebot-vehicle-02"):
        with pytest.raises(IdentityError):
            load_identity(identity_config(vehicle_id=forbidden), allow_placeholder_password=True)
    with pytest.raises(IdentityError):
        load_identity(identity_config(username="someone-else"))
    bad = identity_config().with_suffix(".json.bad")
    bad.write_text("not json")
    with pytest.raises(IdentityError):
        load_identity(bad)
    with pytest.raises(IdentityError):
        load_identity(identity_config(vehicle_id="has space"))


def test_identity_rejects_legacy_env(identity_config, monkeypatch, tmp_path: Path):
    config = identity_config()
    for name in ("FIREBOT_VEHICLE_ID", "BRIDGE_VEHICLE_ID"):
        monkeypatch.setenv(name, "R001")
        with pytest.raises(IdentityError, match="legacy identity env"):
            load_identity(config, allow_placeholder_password=True)
        monkeypatch.delenv(name)


def test_identity_never_reads_bridge_env(tmp_path: Path):
    # 即使 bridge.env 内容碰巧是合法 JSON，也必须是"从未读取"级别的拒绝。
    bridge = tmp_path / "bridge.env"
    bridge.write_text(json.dumps({"vehicle_id": VEHICLE, "mqtt_username": VEHICLE}))
    with pytest.raises(IdentityError, match="legacy identity source"):
        load_identity(bridge)


def test_identity_placeholder_password_gate(identity_config, tmp_path: Path):
    with pytest.raises(IdentityError, match="placeholder"):
        load_identity(identity_config())
    real = load_identity(identity_config(password="0p3r4t0r!device-01"))
    assert real.vehicle_id == VEHICLE
    acceptance = load_identity(identity_config(), allow_placeholder_password=True)
    assert acceptance.vehicle_id == VEHICLE


# ---------------------------------------------------------------------------
# D. 本地隔离验收（进程内 FakeBroker）
# ---------------------------------------------------------------------------


def test_broker_rejects_wrong_credentials(identity_config, broker, tmp_path: Path):
    local = broker(expected_username=VEHICLE, expected_password="right-password")
    identity = load_identity(
        identity_config(port=local.port, password="wrong-password"),
    )
    transport = MqttTransport(identity, TransportConfig())
    transport.connect()
    assert transport.wait_for_connect(2.0) is False
    wait_until(lambda: local.connect_attempts, 5.0, "connect attempt recorded")
    assert local.connect_attempts[0]["username"] == VEHICLE
    assert local.connect_attempts[0]["password"] == "wrong-password"
    assert local.messages == []  # 被拒绝后未发送任何数据
    assert local.accepted_sessions == 0


def test_agent_gives_up_after_failed_auth(identity_config, broker, tmp_path: Path):
    local = broker(expected_password="right-password")
    identity = load_identity(identity_config(port=local.port, password="wrong-password"))
    transport = MqttTransport(
        identity,
        TransportConfig(reconnect_min_seconds=0.01, rng=random.Random(0)),
    )
    config = AgentConfig(
        status_interval_seconds=0.2,
        telemetry_interval_seconds=0.3,
        host_metrics_path=tmp_path / "host.jsonl",
        max_reconnect_attempts=2,
    )
    agent = ReadOnlyAgent(
        identity, transport, config, {"chassis": ("firebot_chassis",)}, threading.Event()
    )
    assert agent.run() == 4
    assert local.messages == []
    assert len(local.connect_attempts) >= 3  # 初连 + 2 次重试


def test_transport_publish_contract_and_duplicate_status(identity_config, broker, tmp_path: Path):
    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(identity, TransportConfig())
    transport.connect()
    assert transport.wait_for_connect(5.0) is True
    # G1-R0 A2：QoS1 发布必须返回 broker PUBACK 确认结果。
    assert transport.publish_availability("online") is True
    status_a = build_status({"chassis": False}, "2026-11-11T05:00:00+00:00")
    status_b = build_status({"chassis": False}, "2026-11-11T05:00:05+00:00")
    assert transport.publish_status(status_a) is True
    assert transport.publish_status(status_b) is True  # 重复状态：retained last-wins，幂等
    assert transport.publish_telemetry(build_telemetry("2026-11-11T05:00:00+00:00")) is True
    wait_until(lambda: len(local.messages) >= 4, 5.0, "all publishes delivered")

    def by_kind(kind: str):
        return [m for m in local.messages if m["topic"] == topic(VEHICLE, kind)]

    availability = by_kind("availability")[0]
    assert availability["qos"] == 1 and availability["retain"] is True
    assert availability["payload"] == b"online"
    statuses = by_kind("status")
    assert len(statuses) == 2
    assert all(m["qos"] == 1 and m["retain"] is True for m in statuses)
    assert all(Status.model_validate(json.loads(m["payload"])) for m in statuses)
    retained_key = topic(VEHICLE, "status")
    assert local.retained[retained_key] == json.dumps(status_b).encode()  # 最新一条生效
    telemetry = by_kind("telemetry")[0]
    assert telemetry["qos"] == 0 and telemetry["retain"] is False
    assert json.loads(telemetry["payload"])["pose"] is None

    assert transport.graceful_offline() is True  # A2：offline 已获 PUBACK 确认
    wait_until(lambda: not transport.connected, 3.0, "clean disconnect")
    assert local.lwt_emissions == []  # 正常退出：干净断开，不触发 LWT
    wait_until(lambda: local.retained.get(topic(VEHICLE, "availability")) == b"offline", 3.0)


def test_agent_loop_in_process(identity_config, broker, tmp_path: Path):
    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(
        identity,
        TransportConfig(reconnect_min_seconds=0.01, rng=random.Random(0)),
    )
    stop_event = threading.Event()
    agent = ReadOnlyAgent(
        identity,
        transport,
        AgentConfig(
            status_interval_seconds=0.2,
            telemetry_interval_seconds=0.3,
            host_metrics_path=tmp_path / "host.jsonl",
        ),
        {"chassis": ("firebot_chassis",)},
        stop_event,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()), daemon=True)
    thread.start()
    wait_until(
        lambda: any(m["topic"] == topic(VEHICLE, "status") for m in local.messages),
        10.0,
        "status published",
    )
    stop_event.set()
    thread.join(10.0)
    assert result == [0]
    assert (tmp_path / "host.jsonl").is_file()  # host 指标默认只写本地
    with (tmp_path / "host.jsonl").open() as handle:
        local_host = json.loads(handle.readline())
    assert local_host["uptime_seconds"] >= 0 and local_host["os"]["kernel"]
    assert local.lwt_emissions == []
    assert local.subscriptions == []  # 从未订阅任何 topic


def test_guard_refuses_when_legacy_bridge_running(tmp_path: Path):
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "777"
    pid_dir.mkdir(parents=True)
    pid_dir.joinpath("cmdline").write_bytes(
        b"/opt/firebot/vehicle-bridge\x00--config\x00bridge.env"
    )
    assert legacy_bridge_running(proc_root) is True
    with pytest.raises(OwnerConflictError, match="legacy vehicle-bridge"):
        OwnerGuard(tmp_path / "run", proc_root=proc_root).acquire()


def test_guard_second_instance_lock(tmp_path: Path):
    first = OwnerGuard(tmp_path / "run")
    first.acquire()
    with pytest.raises(OwnerConflictError, match="another readonly agent instance"):
        OwnerGuard(tmp_path / "run").acquire()
    first.release()
    second = OwnerGuard(tmp_path / "run")
    second.acquire()  # 释放后可再次获取
    second.release()


def test_transport_structurally_cannot_subscribe(identity_config, tmp_path: Path):
    identity = load_identity(identity_config(), allow_placeholder_password=True)
    transport = MqttTransport(identity, TransportConfig())
    with pytest.raises(SubscribeForbiddenError):
        transport.subscribe(topic(VEHICLE, "cmd"))
    # TLS 严格性：绝不 insecure，服务端证书必须可验证，且配置 CA 已装入信任库。
    context = transport._client._ssl_context  # noqa: SLF001
    assert transport._client._tls_insecure is False  # noqa: SLF001
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] >= 1
    # G1-R0 A2：队列/在途上限收紧，断线重试不会无界堆积。
    assert transport._client._max_queued_messages == 64  # noqa: SLF001
    assert transport._client._max_inflight_messages == 8  # noqa: SLF001


def test_backoff_delay_bounded():
    # G1-R0 A2：重连退避封顶（max + 10% 抖动），长期断线重试资源消耗有界。
    config = TransportConfig(
        reconnect_min_seconds=1.0, reconnect_max_seconds=30.0, rng=random.Random(0)
    )
    for attempt in (0, 1, 5, 100):
        delay = config.backoff_delay(attempt)
        assert delay <= 30.0 * 1.1
        assert delay >= 1.0


def test_validate_interval_gate():
    # G1-R0 A1：负数/0/NaN/Inf/低于下限一律拒绝。
    for bad in (-1.0, 0.0, 0.5, float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            validate_interval("--status-interval", bad)
    assert validate_interval("--status-interval", 1.0) == 1.0
    assert validate_interval("--status-interval", 3.0) == 3.0


def test_cli_rejects_invalid_intervals(identity_config, tmp_path: Path, capsys):
    # G1-R0 A1：CLI 边界门禁，退出码 2（argparse error）。
    config = identity_config()
    for bad in ("-1", "0", "nan", "0.1"):
        with pytest.raises(SystemExit) as excinfo:
            agent_main(["--config", str(config), "--status-interval", bad])
        assert excinfo.value.code == 2
        capsys.readouterr()  # 清理 argparse 错误输出


def test_qos1_publish_unconfirmed_when_broker_never_acks(identity_config, broker, tmp_path: Path):
    # G1-R0 A2：broker 收包但不回 PUBACK → 发布必须返回 False，不得宣称成功。
    local = broker(ack_publishes=False)
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(identity, TransportConfig(publish_confirm_timeout_seconds=0.5))
    transport.connect()
    assert transport.wait_for_connect(5.0) is True
    assert transport.publish_availability("online") is False
    assert (
        transport.publish_status(build_status({"chassis": False}, "2026-11-11T05:00:00+00:00"))
        is False
    )
    assert transport.graceful_offline() is False  # offline 未确认 → 不得宣称已发布


def test_agent_does_not_claim_unconfirmed_offline(identity_config, broker, tmp_path, caplog):
    # G1-R0 A2：SIGTERM 语义下 retained offline 未被确认时，日志不得宣称成功。
    local = broker(ack_publishes=False)
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(
        identity,
        TransportConfig(
            reconnect_min_seconds=0.01,
            rng=random.Random(0),
            publish_confirm_timeout_seconds=0.3,
            offline_publish_timeout_seconds=0.3,
        ),
    )
    stop_event = threading.Event()
    agent = ReadOnlyAgent(
        identity,
        transport,
        AgentConfig(
            status_interval_seconds=0.3,
            telemetry_interval_seconds=1.0,
            host_metrics_path=tmp_path / "host.jsonl",
        ),
        {"chassis": ("firebot_chassis",)},
        stop_event,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()), daemon=True)
    thread.start()
    wait_until(lambda: transport.connected, 10.0, "connected")
    time.sleep(0.5)  # 让未确认的 status 发布发生
    stop_event.set()
    thread.join(10.0)
    assert result == [0]
    log_text = caplog.text
    assert "UNCONFIRMED" in log_text  # 只记未确认，不宣称成功
    assert "graceful offline published and broker-confirmed" not in log_text


def test_status_freshness_and_short_disconnect_recovery(identity_config, broker, tmp_path: Path):
    # G1-R0 A1：默认 3s 周期下 status 时间戳必须满足云端 8s stale 门限；
    # 短断连后恢复，新 status 依然新鲜（不遗留过期状态）。
    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(
        identity, TransportConfig(reconnect_min_seconds=0.01, rng=random.Random(0))
    )
    stop_event = threading.Event()
    agent = ReadOnlyAgent(
        identity,
        transport,
        AgentConfig(
            status_interval_seconds=1.0,
            telemetry_interval_seconds=3.0,
            host_metrics_path=tmp_path / "host.jsonl",
        ),
        {"chassis": ("firebot_chassis",)},
        stop_event,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()), daemon=True)
    thread.start()

    def statuses():
        return [
            json.loads(m["payload"])
            for m in local.messages
            if m["topic"] == topic(VEHICLE, "status")
        ]

    wait_until(lambda: len(statuses()) >= 2, 10.0, "two statuses")
    for status in statuses():
        ts = datetime.fromisoformat(status["ts"])
        assert 0 <= (datetime.now(UTC) - ts).total_seconds() <= 8.0  # 云端 stale 门限内

    sessions_before = local.accepted_sessions
    local.abort_clients()  # 短断连
    wait_until(lambda: local.accepted_sessions >= sessions_before + 1, 10.0, "reconnected")
    before = len(statuses())
    wait_until(lambda: len(statuses()) > before, 10.0, "status resumed after reconnect")
    latest_ts = datetime.fromisoformat(statuses()[-1]["ts"])
    assert 0 <= (datetime.now(UTC) - latest_ts).total_seconds() <= 8.0  # 恢复后依然新鲜
    stop_event.set()
    thread.join(10.0)
    assert result == [0]


def test_identity_rejects_open_permissions(identity_config, tmp_path: Path):
    # G1-R1：凭证权限受检——禁止组写与"其他"任意位。
    # 允许 root:服务组 0640（非 root 服务用户组内可读、Agent 不可写、他人不可读）
    # 与 0600；拒绝 0644（其他可读）与 0660（组可写）。
    config = identity_config()
    for bad_mode in (0o644, 0o666, 0o660):
        config.chmod(bad_mode)
        with pytest.raises(IdentityError, match="permissions too open"):
            load_identity(config, allow_placeholder_password=True)
    for good_mode in (0o600, 0o640):
        config.chmod(good_mode)
        assert load_identity(config, allow_placeholder_password=True).vehicle_id == VEHICLE


def test_guard_sigkill_releases_lock(tmp_path: Path):
    # G1-R1：SIGKILL 后 flock 由内核自动释放——进程消失即可再次启动，
    # lock 文件存在不等于锁被持有（真实锁获取验证）。
    lock_code = (
        "import fcntl,time,os\n"
        "fd=os.open({!r}, os.O_RDWR|os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "time.sleep(30)"
    ).format(str(tmp_path / "agent.lock"))
    lock_holder = subprocess.Popen([sys.executable, "-c", lock_code])
    time.sleep(0.5)
    guard = OwnerGuard(tmp_path)
    with pytest.raises(OwnerConflictError):
        guard.acquire()
    lock_holder.kill()  # SIGKILL：不经过任何释放逻辑
    lock_holder.wait(timeout=5)
    assert (tmp_path / "agent.lock").exists()  # 文件仍在……
    guard.acquire()  # ……但真实 flock 已随进程消亡释放
    guard.release()


def test_agent_detects_legacy_bridge_at_runtime(identity_config, broker, tmp_path: Path):
    # G1-R1：旧 Bridge 在 Agent 运行期间被重新启动 → 运行期 owner 冲突检测，
    # 安全处置：发布 offline、退出码 3，systemd Restart=on-failure 接手后由启动守卫持续拒绝。
    from services.vehicle_agent_v1_readonly.agent import OWNER_CHECK_INTERVAL_SECONDS

    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(
        identity, TransportConfig(reconnect_min_seconds=0.01, rng=random.Random(0))
    )
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    stop_event = threading.Event()
    agent = ReadOnlyAgent(
        identity,
        transport,
        AgentConfig(
            status_interval_seconds=1.0,
            telemetry_interval_seconds=2.0,
            host_metrics_path=tmp_path / "host.jsonl",
        ),
        {"chassis": ("firebot_chassis",)},
        stop_event,
        proc_root=proc_root,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()), daemon=True)
    thread.start()
    wait_until(
        lambda: any(m["topic"] == topic(VEHICLE, "status") for m in local.messages),
        10.0,
        "status before conflict",
    )
    # 旧 Bridge 在运行期"重启"：伪造其进程 cmdline 出现在 /proc
    pid_dir = proc_root / "4242"
    pid_dir.mkdir()
    pid_dir.joinpath("cmdline").write_bytes(b"/opt/firebot/vehicle-bridge\x00--restart")
    thread.join(timeout=OWNER_CHECK_INTERVAL_SECONDS + 10.0)
    assert not thread.is_alive()
    assert result == [3]  # 安全处置：退出码 3
    statuses = [m for m in local.messages if m["topic"] == topic(VEHICLE, "status")]
    wait_until(
        lambda: local.retained.get(topic(VEHICLE, "availability")) == b"offline",
        5.0,
        "offline published on conflict",
    )
    # 冲突后不再有新的 status 上行（离线让位）
    last_ts = statuses[-1]
    time.sleep(1.2)
    assert statuses[-1] == last_ts


def test_no_control_paths_in_source():
    package = REPO_ROOT / "services" / "vehicle_agent_v1_readonly"
    sources = sorted(package.glob("*.py"))
    assert sources, "agent package sources missing"
    for source in sources:
        text = source.read_text(encoding="utf-8").lower()
        assert "/cmd_vel" not in text, f"control topic reference in {source.name}"
        assert "modbus" not in text, f"modbus reference in {source.name}"
        assert "import ros" not in text and "from ros" not in text, f"ros import in {source.name}"
        assert "gpio" not in text, f"gpio reference in {source.name}"
    combined = "\n".join(s.read_text(encoding="utf-8") for s in sources)
    # 全包唯一订阅路径是禁止订阅的 stub（反证保证）。
    assert combined.count("subscribe(") == 1, "subscribe must only exist as the forbidden stub"


# ---------------------------------------------------------------------------
# D. 本地隔离验收（真实子进程 + 进程内 FakeBroker，全生命周期）
# ---------------------------------------------------------------------------


def _agent_env() -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("FIREBOT_VEHICLE_ID", "BRIDGE_VEHICLE_ID")
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _start_agent(config: Path, run_dir: Path) -> subprocess.Popen:
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
            "1.1",
            "--telemetry-interval",
            "1.3",
            "--connect-wait",
            "2",
            "--allow-placeholder-credentials",
        ],
        cwd=str(REPO_ROOT),
        env=_agent_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_agent(proc: subprocess.Popen) -> tuple[str, str]:
    proc.terminate()
    proc.wait(timeout=10)
    stdout, stderr = proc.communicate()
    assert proc.returncode == 0, f"agent exit={proc.returncode}\n{stderr}"
    return stdout, stderr


def test_e2e_lifecycle_restart_and_owner_conflict(identity_config, broker, tmp_path: Path):
    local = broker()
    config = identity_config(port=local.port)
    run_dir = tmp_path / "run"
    first = _start_agent(config, run_dir)
    try:
        wait_until(
            lambda: any(m["topic"] == topic(VEHICLE, "status") for m in local.messages),
            20.0,
            "first instance status",
        )
        topics = {m["topic"] for m in local.messages}
        assert topics <= {
            topic(VEHICLE, "availability"),
            topic(VEHICLE, "status"),
            topic(VEHICLE, "telemetry"),
        }, f"unexpected topics: {topics}"
        assert local.subscriptions == []  # 永不订阅（含 cmd）
        status = json.loads(
            [m for m in local.messages if m["topic"] == topic(VEHICLE, "status")][-1]["payload"]
        )
        assert status["online"] is True
        assert status["programs"]["agent"] is True
        assert status["programs"]["chassis"] is False  # 真实 001 未接入底盘
        assert "host" not in status  # 扩展默认关闭
        telemetry = json.loads(
            [m for m in local.messages if m["topic"] == topic(VEHICLE, "telemetry")][0]["payload"]
        )
        assert telemetry["pose"] is None and telemetry["battery"] is None

        # 第二个实例：同一 run-dir，必须拒绝成为第二个通信 owner。
        assert (
            agent_main(
                [
                    "--config",
                    str(config),
                    "--run-dir",
                    str(run_dir),
                    "--allow-placeholder-credentials",
                ]
            )
            == EXIT_OWNER_CONFLICT
        )
        assert local.accepted_sessions == 1
    finally:
        stdout, stderr = _stop_agent(first)
    assert "graceful offline published" in stderr
    wait_until(lambda: local.retained.get(topic(VEHICLE, "availability")) == b"offline", 5.0)
    assert local.lwt_emissions == []  # 正常退出无 LWT

    # 重启行为：再次启动 → 重新上线（lock 已释放）。
    second = _start_agent(config, run_dir)
    try:
        wait_until(
            lambda: local.retained.get(topic(VEHICLE, "availability")) == b"online",
            10.0,
            "restart online",
        )
        assert local.accepted_sessions == 2
    finally:
        _stop_agent(second)


def test_e2e_disconnect_recovery_and_lwt(identity_config, broker, tmp_path: Path):
    local = broker()
    config = identity_config(port=local.port)
    proc = _start_agent(config, tmp_path / "run")
    try:
        wait_until(
            lambda: any(m["topic"] == topic(VEHICLE, "status") for m in local.messages),
            20.0,
            "initial status",
        )
        sessions_before = local.accepted_sessions
        local.abort_clients()  # 模拟网络中断：异常断线必须触发 LWT 并由 Agent 重连
        wait_until(lambda: len(local.lwt_emissions) >= 1, 10.0, "LWT emitted")
        assert local.lwt_emissions[0] == (topic(VEHICLE, "availability"), "offline")
        wait_until(lambda: local.accepted_sessions >= sessions_before + 1, 20.0, "reconnected")
        wait_until(
            lambda: local.retained.get(topic(VEHICLE, "availability")) == b"online",
            10.0,
            "availability online after reconnect",
        )
    finally:
        stdout, stderr = _stop_agent(proc)
    assert "connection lost" in stderr
    assert local.retained[topic(VEHICLE, "availability")] == b"offline"  # 最终优雅退出


def test_e2e_wrong_credentials_rejected(identity_config, broker, tmp_path: Path):
    local = broker(expected_password="right-password")
    config = identity_config(port=local.port, password="wrong-password")
    proc = _start_agent(config, tmp_path / "run")
    try:
        wait_until(lambda: len(local.connect_attempts) >= 2, 20.0, "repeated rejected attempts")
        assert local.messages == []  # 从未发送任何数据
        assert local.accepted_sessions == 0
        assert all(attempt["password"] == "wrong-password" for attempt in local.connect_attempts)
    finally:
        stdout, stderr = _stop_agent(proc)
    assert "connect failed" in stderr


def test_e2e_wrong_ca_rejected(identity_config, broker, tmp_path: Path):
    # broker 提供 other_ca 签发的证书；Agent 信任 test ca → 握手必须失败且不发送数据。
    local = broker(certfile="other_server.crt", keyfile="other_server.key")
    config = identity_config(port=local.port)
    proc = _start_agent(config, tmp_path / "run")
    try:
        wait_until(lambda: local.handshake_failures >= 1, 20.0, "TLS handshake rejected")
        time.sleep(0.5)
        assert local.messages == []
        assert local.accepted_sessions == 0
    finally:
        stdout, stderr = _stop_agent(proc)
    assert "connect failed" in stderr


def test_live_availability_online_non_retained_contract(identity_config, broker, tmp_path: Path):
    """G1-P2-L0 A1：周期性 live availability online——QoS1、retain=False、PUBACK 确认。"""
    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    # CI 冷启动负载下 PUBACK 可能延迟：放宽确认窗口，避免时序假阴性
    transport = MqttTransport(
        identity, TransportConfig(publish_confirm_timeout_seconds=5.0)
    )
    transport.connect()
    assert transport.wait_for_connect(10.0) is True
    assert transport.publish_availability_live() is True
    wait_until(
        lambda: any(m["topic"] == topic(VEHICLE, "availability") for m in local.messages), 5.0
    )
    live = [m for m in local.messages if m["topic"] == topic(VEHICLE, "availability")][0]
    assert live["qos"] == 1 and live["retain"] is False
    assert live["payload"] == b"online"
    # 非 retained 不得污染 broker 存储的 retained 值（守卫语义保留）
    assert local.retained.get(topic(VEHICLE, "availability")) is None


def test_agent_loop_publishes_periodic_live_online(identity_config, broker, tmp_path: Path):
    """G1-P2-L0 A1：运行循环周期性发布 live online（非 retained），约 5s 间隔。"""
    local = broker()
    identity = load_identity(identity_config(port=local.port), allow_placeholder_password=True)
    transport = MqttTransport(
        identity,
        TransportConfig(
            reconnect_min_seconds=0.01,
            publish_confirm_timeout_seconds=5.0,
            rng=random.Random(0),
        ),
    )
    stop_event = threading.Event()
    agent = ReadOnlyAgent(
        identity,
        transport,
        AgentConfig(
            status_interval_seconds=0.2,
            telemetry_interval_seconds=0.3,
            host_metrics_path=tmp_path / "host.jsonl",
        ),
        {"chassis": ("firebot_chassis",)},
        stop_event,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(agent.run()), daemon=True)
    thread.start()
    wait_until(
        lambda: sum(
            1
            for m in local.messages
            if m["topic"] == topic(VEHICLE, "availability") and m["retain"] is False
        )
        >= 2,
        25.0,
        "periodic live online published (>=2 条)",
    )
    stop_event.set()
    thread.join(10.0)
    assert result == [0]
    live = [
        m
        for m in local.messages
        if m["topic"] == topic(VEHICLE, "availability") and m["retain"] is False
    ]
    assert len(live) >= 2
    assert all(m["payload"] == b"online" for m in live)
    # retained online 每次建连发布一次（重连时再次发布属正常合同行为，不计次数）；
    # 关键合同：周期性 live 消息全部 retain=False，不污染 broker 存储的 retained 值。
    retained_online = [
        m
        for m in local.messages
        if m["topic"] == topic(VEHICLE, "availability")
        and m["retain"] is True
        and m["payload"] == b"online"
    ]
    assert len(retained_online) >= 1
