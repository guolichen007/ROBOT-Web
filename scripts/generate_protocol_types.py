"""DEPRECATED: 1.2 frozen artifacts cannot be regenerated/accepted by refreshing the stamp.

This script no longer generates anything and never writes .generated.sha256.
It now runs the same read-only verification as scripts/check_protocol_drift.py.
Use that script directly in CI/gates.
"""

from __future__ import annotations

import sys

from check_protocol_drift import (
    STAMP,
    assert_contract_markers,
    assert_schema_13_valid,
    digest,
)


def main() -> int:
    print("generate_protocol_types.py is deprecated; running read-only drift verification only.")
    assert_contract_markers()
    assert_schema_13_valid()
    expected = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    current = digest()
    if current != expected:
        print(
            "ERROR: 1.2 frozen artifacts cannot be regenerated/accepted by refreshing the stamp.",
            file=sys.stderr,
        )
        print(
            "Do NOT run this script to update the stamp. Fix the source drift in "
            "packages/generated-python / packages/generated-typescript against "
            "packages/protocol-schemas/firebot-message-1.2.schema.json instead.",
            file=sys.stderr,
        )
        return 1
    print(f"protocol schema/model stamp OK: {current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
