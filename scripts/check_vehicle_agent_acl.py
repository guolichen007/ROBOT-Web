"""Conservative v1 ACL release gate; never echo credentials or ACL contents."""

import argparse
import sys
from pathlib import Path


def validate_acl(content: str, platform_user: str = "platform") -> bool:
    platform = set()
    patterns = set()
    user = None
    for raw in content.splitlines():
        # '#' is also an MQTT wildcard (e.g. pattern deny #), not an inline comment.
        if raw.lstrip().startswith("#"):
            continue
        parts = raw.split()
        if not parts:
            continue
        if parts[0] == "user" and len(parts) == 2:
            user = parts[1]
        elif parts[0] == "topic" and user == platform_user and len(parts) == 3:
            if parts[1] == "deny":
                return False
            platform.add(tuple(parts[1:]))
        elif parts[0] == "pattern" and len(parts) == 3:
            if parts[1] == "deny":
                return False
            patterns.add(tuple(parts[1:]))
    required_platform = {
        ("readwrite", "_platform/vehicle-agent-v1/+/+"),
        ("readwrite", "v1/robot/+/+"),
    }
    required_vehicle = {("read", "v1/robot/%u/cmd")} | {
        ("write", f"v1/robot/%u/{kind}")
        for kind in ("ack", "status", "telemetry", "availability", "event")
    }
    return required_platform <= platform and required_vehicle <= patterns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="active ACL path, or - for broker-container stdin")
    parser.add_argument("--platform-user", default="platform")
    args = parser.parse_args()
    try:
        content = (
            sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        )
        ok = validate_acl(content, args.platform_user)
    except (OSError, UnicodeError):
        ok = False
    print("MOSQUITTO_V1_ACL=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
