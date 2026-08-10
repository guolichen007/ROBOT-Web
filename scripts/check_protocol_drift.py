from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "packages/protocol-schemas/firebot-message-1.1.schema.json"
STAMP = ROOT / "packages/protocol-schemas/.generated.sha256"


def digest() -> str:
    payload = json.dumps(json.loads(SCHEMA.read_text(encoding="utf-8")), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    expected = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    current = digest()
    if current != expected:
        raise SystemExit(
            "Protocol generated models are stale. Run scripts/generate_protocol_types.py"
        )
    print(f"protocol schema/model stamp OK: {current}")
