from __future__ import annotations

from check_protocol_drift import STAMP, assert_contract_markers, digest

if __name__ == "__main__":
    assert_contract_markers()
    STAMP.write_text(digest() + "\n", encoding="utf-8")
    print(f"updated {STAMP.as_posix()}")
