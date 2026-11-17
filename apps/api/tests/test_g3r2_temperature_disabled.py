"""G3-R2 单元合同：独立温度数值停用（无 MQTT / 无 DB 依赖，CI backend 必跑）。

- ir_telemetry_sim 默认停用：未同时给出 FIREBOT_LEGACY_THERMAL_COMPAT_TEST=1
  与 --legacy-compat-test 时退出码 3，且在读取身份文件 / 建立 MQTT 连接之前返回
  （零网络副作用）。
- sim_presence 结构上不调用 publish_telemetry，只发 availability / status；
  status 不声明 thermal 测温程序。
- 两个工具都拒绝真实车辆身份。
"""

from __future__ import annotations

import json

import pytest

from services.ir_telemetry_sim import main as ir_sim
from services.sim_presence import main as presence


class _NoNetwork:
    def __init__(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("must not construct MQTT transport")


@pytest.mark.parametrize(
    "argv,env",
    [
        (["--config", "/nonexistent.json"], None),
        (["--legacy-compat-test", "--config", "/nonexistent.json"], None),
        (["--config", "/nonexistent.json"], "1"),
        (["--legacy-compat-test", "--config", "/nonexistent.json"], "0"),
    ],
)
def test_ir_sim_disabled_before_identity_and_network(monkeypatch, capsys, argv, env):
    monkeypatch.delenv(ir_sim.COMPAT_ENV, raising=False)
    if env is not None:
        monkeypatch.setenv(ir_sim.COMPAT_ENV, env)
    monkeypatch.setattr(ir_sim, "MqttTransport", _NoNetwork)
    monkeypatch.setattr(
        ir_sim, "load_identity", lambda *a, **k: pytest.fail("identity must not be read")
    )
    assert ir_sim.main(argv) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disabled" in captured.err and "no independent temperature sensor" in captured.err


def test_ir_sim_compat_mode_still_requires_both_switches(monkeypatch):
    """兼容性测试模式：两项开关齐备才进入原有身份校验（此处身份文件不存在 → 退出码 2）。"""
    monkeypatch.setenv(ir_sim.COMPAT_ENV, "1")
    monkeypatch.setattr(ir_sim, "MqttTransport", _NoNetwork)
    assert ir_sim.main(["--legacy-compat-test", "--config", "/nonexistent.json"]) == 2


class _RecordingTransport:
    instances: list[_RecordingTransport] = []

    def __init__(self, identity, config):
        self.calls: list[tuple[str, object]] = []
        _RecordingTransport.instances.append(self)

    def connect(self):
        self.calls.append(("connect", None))

    def wait_for_connect(self, timeout):
        return True

    def publish_availability_live(self):
        self.calls.append(("availability", "online"))
        return True

    def publish_status(self, data):
        self.calls.append(("status", data))
        return True

    def publish_telemetry(self, data):  # pragma: no cover - 被调用即失败
        raise AssertionError("sim_presence must never publish telemetry")

    def loop(self, timeout=0.1):
        pass

    def disconnect(self):
        self.calls.append(("disconnect", None))


def _identity(vehicle_id: str):
    from pathlib import Path

    from services.vehicle_agent_v1_readonly.identity import DeviceIdentity

    return DeviceIdentity(
        vehicle_id=vehicle_id,
        display_name="SIM",
        mqtt_host="127.0.0.1",
        mqtt_port=1,
        mqtt_username=vehicle_id,
        mqtt_password="unit-test-only",
        ca_cert_path=Path("/nonexistent-ca.crt"),
    )


def test_presence_publishes_only_availability_and_status(monkeypatch, capsys):
    _RecordingTransport.instances.clear()
    monkeypatch.setattr(presence, "MqttTransport", _RecordingTransport)
    monkeypatch.setattr(presence, "load_identity", lambda *a, **k: _identity("firebot-sim-ir-01"))
    assert presence.main(["--config", "x.json"]) == 0
    (transport,) = _RecordingTransport.instances
    kinds = [kind for kind, _ in transport.calls]
    assert kinds == ["connect", "availability", "status", "disconnect"]
    status = dict(transport.calls)["status"]
    assert "thermal" not in status["programs"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["telemetry_sent"] == 0 and out["vehicle_id"] == "firebot-sim-ir-01"


@pytest.mark.parametrize(
    "vehicle_id", ["firebot-vehicle-01", "firebot-vehicle-03", "R001", "R002", "firebot-sim-"]
)
def test_presence_refuses_non_synthetic_identity(monkeypatch, vehicle_id):
    monkeypatch.setattr(presence, "MqttTransport", _NoNetwork)
    monkeypatch.setattr(presence, "load_identity", lambda *a, **k: _identity(vehicle_id))
    assert presence.main(["--config", "x.json"]) == 2


def test_presence_namespace_matches_cloud_rule():
    from app.core.config import REAL_VEHICLE_IDS, SYNTHETIC_ID_PATTERN, is_synthetic_namespace

    assert presence.SYNTHETIC_ID_PATTERN.pattern == SYNTHETIC_ID_PATTERN.pattern
    assert presence.REAL_VEHICLE_IDS == REAL_VEHICLE_IDS
    for vid in ("firebot-sim-ir-01", "firebot-sim-x", "R001", "firebot-vehicle-01", "firebot-sim-"):
        assert presence.is_sim_identity(vid) == is_synthetic_namespace(vid)
