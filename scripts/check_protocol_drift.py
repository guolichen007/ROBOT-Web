from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "packages/protocol-schemas/firebot-message-1.2.schema.json"
PYTHON_MODEL = ROOT / "packages/generated-python/firebot_protocol.py"
TYPESCRIPT_MODEL = ROOT / "packages/generated-typescript/firebot-protocol.ts"
STAMP = ROOT / "packages/protocol-schemas/.generated.sha256"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest() -> str:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode()
    generated = PYTHON_MODEL.read_bytes() + b"\0" + TYPESCRIPT_MODEL.read_bytes()
    return hashlib.sha256(canonical + b"\0" + generated).hexdigest()


def assert_contract_markers() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    assert_contract_markers()
    expected = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    current = digest()
    if current != expected:
        raise SystemExit(
            "Protocol generated models are stale. Run scripts/generate_protocol_types.py"
        )
    print(f"protocol schema/model stamp OK: {current}")
