import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from services.protocol import validate_message

ROOT = Path(__file__).resolve().parents[3]
HANDOFF = ROOT / "integration/ros2"


def test_manifest_freezes_contract_and_topics() -> None:
    manifest = json.loads((HANDOFF / "机器人集成清单.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "1.2.0"
    assert manifest["schema_version"] == "1.2"
    assert len(manifest["topics"]) == 10
    assert {item["name"] for item in manifest["commands"]} == {
        "manual_control",
        "stop_motion",
        "emergency_stop",
        "reset_estop",
        "patrol",
        "extinguish",
        "return_dock",
        "cancel_task",
    }


def test_handoff_schema_is_canonical() -> None:
    canonical = ROOT / "packages/protocol-schemas/firebot-message-1.2.schema.json"
    delivered = HANDOFF / "schemas/灭火机器人消息-1.2模式.json"
    assert (
        hashlib.sha256(canonical.read_bytes()).digest()
        == hashlib.sha256(delivered.read_bytes()).digest()
    )


def test_all_handoff_examples_conform() -> None:
    files = list((HANDOFF / "examples").glob("*.json"))
    assert len(files) >= 19
    for path in files:
        validate_message(json.loads(path.read_text(encoding="utf-8")))


def test_invalid_schema_vector_is_rejected() -> None:
    payload = json.loads((HANDOFF / "test-vectors/无效协议版本.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        validate_message(payload)


def test_duplicate_vector_keeps_identical_command_id_and_payload() -> None:
    first = json.loads(
        (HANDOFF / "test-vectors/重复命令_首次.json").read_text(encoding="utf-8")
    )
    retry = json.loads(
        (HANDOFF / "test-vectors/重复命令_重试.json").read_text(encoding="utf-8")
    )
    assert first == retry
    validate_message(first)
