"""readiness ros-compat 可选化测试：canonical MQTT 下 ros-compat 无心跳仍 ready。"""

from __future__ import annotations

from app.modules.system.router import readiness_payload


class _FakeDb:
    def execute(self, _stmt):
        return None


def _heartbeats(monkeypatch, ros_compat_mode: bool, ros_compat_heartbeat: bool) -> dict:
    from app.core import config as config_mod

    settings = config_mod.get_settings()
    settings.ros_compat_mode = ros_compat_mode

    # 模拟所有基础依赖 OK
    monkeypatch.setattr("app.modules.system.router.bounded_tcp_probe", lambda *a, **k: (True, None))

    class _FakeRedis:
        def __init__(self):
            self.values = {
                "service:mqtt-ingress:heartbeat": "1",
                "service:task-worker:heartbeat": "1",
                "service:command-dispatcher:outbox-heartbeat": "1",
                "service:command-dispatcher:safety-heartbeat": "1",
            }
            if ros_compat_heartbeat:
                self.values["service:ros-compat-adapter:heartbeat"] = "1"

        def get(self, key):
            return self.values.get(key)

    monkeypatch.setattr("app.modules.system.router.get_redis", _FakeRedis)
    return {}


def test_canonical_mqtt_ready_without_ros_compat(monkeypatch) -> None:
    _heartbeats(monkeypatch, ros_compat_mode=False, ros_compat_heartbeat=False)
    payload = readiness_payload(_FakeDb())
    assert payload["ok"] is True
    assert payload["checks"]["ros_compat_adapter"]["required"] is False


def test_ros_compat_required_blocks_ready_without_heartbeat(monkeypatch) -> None:
    _heartbeats(monkeypatch, ros_compat_mode=True, ros_compat_heartbeat=False)
    payload = readiness_payload(_FakeDb())
    assert payload["ok"] is False
    assert payload["checks"]["ros_compat_adapter"]["required"] is True
    assert payload["checks"]["ros_compat_adapter"]["ok"] is False


def test_ros_compat_required_passes_with_heartbeat(monkeypatch) -> None:
    _heartbeats(monkeypatch, ros_compat_mode=True, ros_compat_heartbeat=True)
    payload = readiness_payload(_FakeDb())
    assert payload["ok"] is True
