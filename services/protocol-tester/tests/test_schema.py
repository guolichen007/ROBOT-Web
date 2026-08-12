import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from services.protocol import validate_message

ROOT = Path(__file__).resolve().parents[3]


def test_shared_fixtures_conform() -> None:
    for path in (ROOT / "packages/shared-fixtures").glob("*.json"):
        validate_message(json.loads(path.read_text(encoding="utf-8")))


def test_unknown_schema_is_rejected() -> None:
    payload = json.loads(
        (ROOT / "packages/shared-fixtures/location.json").read_text(encoding="utf-8")
    )
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        validate_message(payload)


def test_command_retain_contract_is_documented() -> None:
    protocol = ROOT / "docs/MQTT协议.md"
    if protocol.exists():
        assert "retain=false" in protocol.read_text(encoding="utf-8")
