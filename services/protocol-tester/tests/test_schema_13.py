"""schema 1.3 服务器双版本兼容回归测试。"""
from __future__ import annotations

import json
import uuid

import pytest
from jsonschema import ValidationError

from services.protocol import validate_message


def _base(**over) -> dict:
    msg = {
        "schema_version": "1.3",
        "message_id": str(uuid.uuid4()),
        "type": "sensor",
        "vehicle_id": "firebot-vehicle-01",
        "boot_id": str(uuid.uuid4()),
        "timestamp": "2026-08-24T00:00:00+00:00",
        "seq": 1,
    }
    msg.update(over)
    return msg


def test_schema_13_sensor_smoke_only() -> None:
    # 1.3：sensor 只要求 smoke，IR 可选、缺失不出现。
    validate_message(_base(type="sensor", smoke=3.5))


def test_schema_13_sensor_with_optional_ir() -> None:
    validate_message(_base(type="sensor", smoke=3.5, bottom_ir=31.2, top_ir_max=36.1))


def test_schema_13_sensor_requires_smoke() -> None:
    with pytest.raises(ValidationError):
        validate_message(_base(type="sensor", bottom_ir=31.2))


def test_schema_13_status_partial_battery_only() -> None:
    # 1.3：status partial，mode/battery/estop_active 均可缺失。
    validate_message(_base(type="status", battery=82.4))


def test_schema_13_command_ack_bridge_adapter_not_connected() -> None:
    validate_message(
        _base(
            type="command_ack",
            command_id="C-0001",
            status="rejected",
            reason_code="BRIDGE_ADAPTER_NOT_CONNECTED",
        )
    )


def test_unknown_schema_version_is_explicitly_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_message(_base(schema_version="9.9"))
