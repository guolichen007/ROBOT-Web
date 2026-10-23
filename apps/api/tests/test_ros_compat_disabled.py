import importlib.util
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


class StopTestLoop(Exception):
    pass


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setenv("ROS_COMPAT_MODE", "false")
    monkeypatch.setattr("paho.mqtt.client.Client", Mock())
    # Avoid replacing pytest's log handlers while importing the service.
    monkeypatch.setattr("app.core.logging.configure_logging", lambda *args: None)
    path = Path(__file__).resolve().parents[3] / "services/ros-compat-adapter/main.py"
    spec = importlib.util.spec_from_file_location("ros_compat_disabled_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.redis = Mock()
    module.Adapter = Mock(side_effect=AssertionError("disabled mode must not instantiate Adapter"))
    assert module.ENABLED is False
    return module


def test_normal_disabled_heartbeat(disabled, monkeypatch):
    monkeypatch.setattr(disabled.time, "sleep", Mock(side_effect=StopTestLoop))
    with pytest.raises(StopTestLoop):
        disabled.disabled_heartbeat_loop()
    disabled.redis.setex.assert_called_once_with(
        "service:ros-compat-adapter:heartbeat", 5, "DISABLED"
    )
    disabled.time.sleep.assert_called_once_with(2)


@pytest.mark.parametrize("fault", [RedisTimeoutError, RedisConnectionError])
def test_disabled_main_survives_redis_failure_and_resumes(disabled, monkeypatch, caplog, fault):
    outcomes = iter([fault("temporary Redis failure"), None, None])
    successful = []

    def setex(*args):
        outcome = next(outcomes)
        if outcome:
            raise outcome
        successful.append(args)

    disabled.redis.setex.side_effect = setex
    sleeps = Mock(side_effect=[None, None, StopTestLoop])
    monkeypatch.setattr(disabled.time, "sleep", sleeps)
    with caplog.at_level("WARNING", logger="ros-compat-adapter"), pytest.raises(StopTestLoop):
        disabled.main()
    assert disabled.redis.setex.call_count == 3
    assert (
        disabled.redis.method_calls
        == [call.setex("service:ros-compat-adapter:heartbeat", 5, "DISABLED")] * 3
    )
    assert successful == [("service:ros-compat-adapter:heartbeat", 5, "DISABLED")] * 2
    assert sleeps.call_count == 3
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert caplog.records[0].exc_info[0] is fault
    disabled.Adapter.assert_not_called()
    disabled.mqtt.Client.assert_not_called()
    assert disabled.ENABLED is False


def test_disabled_mode_does_not_fall_through_to_mqtt(disabled, monkeypatch):
    loop = Mock()
    monkeypatch.setattr(disabled, "disabled_heartbeat_loop", loop)
    disabled.main()
    loop.assert_called_once_with()
    disabled.Adapter.assert_not_called()
    disabled.mqtt.Client.assert_not_called()
