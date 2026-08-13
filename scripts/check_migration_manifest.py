from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "apps" / "api" / "alembic" / "versions"
MANIFEST = VERSIONS / "migration-sha256.json"


def hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(VERSIONS.glob("202608*.py"))
    }


def main() -> None:
    expected = json.loads(MANIFEST.read_text("utf-8"))
    actual = hashes()
    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2), file=sys.stderr)
        raise SystemExit("migration manifest drift: frozen revisions may not be modified")
    print(f"MIGRATION_MANIFEST=PASS revisions={len(actual)}")


if __name__ == "__main__":
    main()
