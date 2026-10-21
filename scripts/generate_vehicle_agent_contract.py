"""生成独立 v1 adapter 合同；不改动 frozen canonical 1.2/1.3。"""

import json
from pathlib import Path

from services.vehicle_agent_v1 import (
    TOPIC_CONTRACT,
    Ack,
    CanonicalCommand,
    Status,
    Telemetry,
    WireCommand,
)

ROOT = Path(__file__).resolve().parents[1]


def contract() -> dict:
    return {
        "version": "vehicle-agent-v1",
        "topics": {
            kind: {"qos": qos, "retain": retain} for kind, (qos, retain) in TOPIC_CONTRACT.items()
        },
        "models": {
            model.__name__: model.model_json_schema()
            for model in (WireCommand, Ack, Status, Telemetry, CanonicalCommand)
        },
    }


if __name__ == "__main__":
    target = ROOT / "packages/protocol-schemas/vehicle-agent-v1.contract.json"
    target.write_text(json.dumps(contract(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
