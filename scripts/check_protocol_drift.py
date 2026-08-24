from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_12 = ROOT / "packages/protocol-schemas/firebot-message-1.2.schema.json"
SCHEMA_13 = ROOT / "packages/protocol-schemas/firebot-message-1.3.schema.json"
PYTHON_MODEL = ROOT / "packages/generated-python/firebot_protocol.py"
TYPESCRIPT_MODEL = ROOT / "packages/generated-typescript/firebot-protocol.ts"
STAMP = ROOT / "packages/protocol-schemas/.generated.sha256"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest() -> str:
    schema = json.loads(SCHEMA_12.read_text(encoding="utf-8"))
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode()
    generated = PYTHON_MODEL.read_bytes() + b"\0" + TYPESCRIPT_MODEL.read_bytes()
    return hashlib.sha256(canonical + b"\0" + generated).hexdigest()


def assert_contract_markers() -> None:
    """1.2 保持 frozen：canonical 生成模型契约必须仍为 1.2。"""
    schema = json.loads(SCHEMA_12.read_text(encoding="utf-8"))
    if schema["$defs"]["vehicleBase"]["properties"]["schema_version"]["const"] != "1.2":
        raise SystemExit("canonical schema_version is not 1.2")
    if (
        schema["$defs"]["capabilities"]["allOf"][1]["properties"]["protocol_version"]["const"]
        != "1.2.0"
    ):
        raise SystemExit("contract_version is not 1.2.0")
    for path in (PYTHON_MODEL, TYPESCRIPT_MODEL):
        text = path.read_text(encoding="utf-8")
        if "1.2" not in text or "target_boot_id" not in text:
            raise SystemExit(f"generated model lacks 1.2 contract markers: {path}")


def assert_schema_13_valid() -> None:
    """1.3 为 current vehicle bridge contract：必须是合法且 capability-driven 的 schema。"""
    schema = json.loads(SCHEMA_13.read_text(encoding="utf-8"))
    if schema["$defs"]["vehicleBase"]["properties"]["schema_version"]["const"] != "1.3":
        raise SystemExit("1.3 schema_version is not 1.3")
    caps = schema["$defs"]["capabilities"]["allOf"][1]["properties"]["protocol_version"]
    if caps.get("enum") and "1.3.0" not in caps["enum"]:
        raise SystemExit("1.3 capabilities protocol_version enum lacks 1.3.0")
    sensor_allof = schema["$defs"]["sensor"]["allOf"]
    if not any("anyOf" in block for block in sensor_allof):
        raise SystemExit("1.3 sensor is not capability-driven (missing anyOf)")
    if any("required" in block and "smoke" in block.get("required", []) for block in sensor_allof):
        raise SystemExit("1.3 sensor must not hard-require smoke")


if __name__ == "__main__":
    assert_contract_markers()
    assert_schema_13_valid()
    expected = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    current = digest()
    if current != expected:
        raise SystemExit(
            "Protocol generated models are stale. Run scripts/generate_protocol_types.py"
        )
    print(f"protocol schema/model stamp OK: {current}")
