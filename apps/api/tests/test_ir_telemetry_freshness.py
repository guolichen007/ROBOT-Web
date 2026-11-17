"""G3 红外温度（V1 telemetry.thermal.max_temp）新鲜度与类型反证。

真实链路代码：vehicle-agent-adapter（Adapter.process）→ 内部 topic → vehicle_agent_ingress.process
→ SensorSample.top_ir_max / Redis robot:<id>:latest.top_ir / agent_state.telemetry。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from scripts.vehicle_agent_mock_runtime import MockRuntime

SIM = "R001"  # MockRuntime 固定车辆；真实组件 E2E 使用 firebot-sim-ir-01


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("APP_ENV", "FIREBOT_MOCK_E2E")}
    os.environ.update(APP_ENV="test", FIREBOT_MOCK_E2E="1")
    value = MockRuntime(tmp_path_factory.mktemp("ir") / "test.sqlite")
    yield value
    value.close()
    for key, previous in old.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def send(runtime, temp, *, ts=None, retained=False):
    data = {
        "pose": None,
        "battery": None,
        "smog": None,
        "thermal": None if temp == "NULL" else {"max_temp": temp},
        "ts": (ts or datetime.now(UTC)).isoformat(),
    }
    runtime.redis.delete(f"vehicle-agent:sample:{SIM}")  # 允许逐条落样本
    runtime.messages.put((f"v1/robot/{SIM}/telemetry", json.dumps(data).encode(), retained))
    try:
        runtime.pump()
    except ValueError:
        # Adapter 校验拒绝（生产中 on_message 记录告警并丢弃）；队列中剩余消息继续处理
        runtime.pump()


def latest(runtime):
    return json.loads(runtime.redis.get(f"robot:{SIM}:latest") or "{}")


def last_sample(runtime):
    from app.db.models import Robot, SensorSample

    with runtime.SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        return db.scalar(
            select(SensorSample)
            .where(SensorSample.robot_id == robot.id)
            .order_by(SensorSample.server_received_at.desc(), SensorSample.id.desc())
            .limit(1)
        )


def state(runtime):
    from app.db.models import Robot

    with runtime.SessionLocal() as db:
        return db.scalar(select(Robot).where(Robot.vehicle_id == SIM)).agent_state_json


@pytest.mark.parametrize("value", [27.5, 42.0, 67.8])
def test_fresh_values_propagate(runtime, value):
    send(runtime, value)
    assert latest(runtime)["top_ir"] == value
    assert last_sample(runtime).top_ir_max == value
    assert state(runtime)["telemetry"]["thermal"]["max_temp"] == value


def test_null_is_explicit_unknown_not_zero(runtime):
    send(runtime, 42.0)
    send(runtime, "NULL")
    assert latest(runtime)["top_ir"] is None  # 新鲜的 null = 当前未知（非 0、非沿用旧值）
    assert last_sample(runtime).top_ir_max is None


def test_stale_ten_minutes_does_not_overwrite_live(runtime):
    send(runtime, 42.0)
    before_sample = last_sample(runtime).id
    send(runtime, 99.0, ts=datetime.now(UTC) - timedelta(minutes=10))
    assert latest(runtime)["top_ir"] == 42.0
    assert state(runtime)["telemetry"]["thermal"]["max_temp"] == 42.0
    sample = last_sample(runtime)
    assert sample.id == before_sample or sample.top_ir_max != 99.0  # 过期值不作为实时样本


def test_out_of_order_older_does_not_overwrite_newer(runtime):
    now = datetime.now(UTC)
    send(runtime, 55.0, ts=now)
    send(runtime, 31.0, ts=now - timedelta(seconds=2))  # 晚到但更旧
    assert latest(runtime)["top_ir"] == 55.0


def test_retained_telemetry_never_becomes_live(runtime):
    send(runtime, 42.0)
    send(runtime, 88.0, retained=True)  # 即使时间戳新鲜
    assert latest(runtime)["top_ir"] == 42.0
    assert state(runtime)["telemetry"]["thermal"]["max_temp"] == 42.0


@pytest.mark.parametrize("bad", ["42.0", True, "hot", 1e9, -400, [42.0], {"v": 1}])
def test_invalid_temperature_type_rejected(runtime, bad):
    send(runtime, 42.0)
    send(runtime, bad)
    assert latest(runtime)["top_ir"] == 42.0
    assert last_sample(runtime).top_ir_max == 42.0
