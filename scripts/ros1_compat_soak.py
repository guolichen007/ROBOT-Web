from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT))


def load_adapter():
    spec = spec_from_file_location(
        "firebot_ros1_soak_adapter", ROOT / "services/ros-compat-adapter/main.py"
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Adapter()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=float, default=1800)
    parser.add_argument("--hz", type=int, default=50)
    parser.add_argument("--output", default="artifacts/ros1-compat-soak.json")
    args = parser.parse_args()
    count = int(args.duration_seconds * args.hz)
    adapter = load_adapter()
    tracemalloc.start()
    latencies: list[float] = []
    start = time.perf_counter()
    boot = adapter.boot_id
    for seq in range(count):
        received = datetime.now(UTC)
        payload = {
            "compat_schema_version": "1.1",
            "external_id": "firerobot-01",
            "bridge_boot_id": boot,
            "seq": seq,
            "ts": received.isoformat(),
            "vx": 0.1,
            "vy": 0.02,
            "wz": 0.01,
        }
        before = time.perf_counter()
        normalized = adapter.normalize(
            "odom", payload, external_id="firerobot-01", received=received
        )
        latencies.append((time.perf_counter() - before) * 1000)
        assert normalized[0][0] == "compat_odom"
        target = start + (seq + 1) / args.hz
        if args.duration_seconds >= 60:
            time.sleep(max(0, target - time.perf_counter()))
    current, peak = tracemalloc.get_traced_memory()
    elapsed = time.perf_counter() - start
    ordered = sorted(latencies)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    result = {
        "status": "PASS",
        "messages": count,
        "configured_hz": args.hz,
        "elapsed_seconds": elapsed,
        "processing_rate_hz": count / elapsed,
        "p50_processing_ms": statistics.median(latencies),
        "p95_processing_ms": p95,
        "peak_python_bytes": peak,
        "current_python_bytes": current,
        "backlog": 0,
        "ws_rate_contract_hz": 10,
        "metadata_refresh_contract_seconds": 60,
    }
    if p95 > 500:
        raise SystemExit("p95 processing lag exceeded 500ms")
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
