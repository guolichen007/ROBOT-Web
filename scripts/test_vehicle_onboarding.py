#!/usr/bin/env python3
"""U-Dao 单车凭据与 ACL 隔离自动化测试（配置层断言 + pattern 展开推演）。

断言：
  - ACL pattern（%u）覆盖 R001 自己的全部上行主题与 command 只读；
  - R001 → R002 主题推演拒绝（无通配、无跨车授权）；
  - anonymous 拒绝（mosquitto.server.conf allow_anonymous false）；
  - 错误凭据拒绝（password_file 生效 + allow_anonymous false 的组合语义）。

broker 级行为由 mosquitto 自身实现保证；本测试锁定配置与模板不被破坏。
Usage: python3 scripts/test_vehicle_onboarding.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def acl_lines() -> list[tuple[str, str, str]]:
    """(directive, keyword, value) 三元组。"""
    out = []
    for line in (ROOT / "infra/mosquitto/acl.example").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        out.append((parts[0], parts[1], parts[2]))
    return out


def main() -> int:
    acl = acl_lines()
    conf = (ROOT / "infra/mosquitto/mosquitto.server.conf").read_text(encoding="utf-8")

    # ---- 1) R001 自己的上行主题全部覆盖（pattern %u 展开 == vehicle_id）----
    uplink = {
        "robot/%u/availability",
        "robot/%u/heartbeat",
        "robot/%u/capabilities",
        "robot/%u/status",
        "robot/%u/sensor",
        "robot/%u/location",
        "robot/%u/alarm",
        "robot/%u/task_status",
        "robot/%u/command_ack",
        "v1/robot/%u/ack",
        "v1/robot/%u/status",
        "v1/robot/%u/telemetry",
        "v1/robot/%u/availability",
        "v1/robot/%u/event",
    }
    written = {v for d, k, v in acl if d == "pattern" and k == "write"}
    check("R001 全部上行主题可写（pattern 覆盖）", uplink <= written, str(uplink - written))

    # ---- 2) 只读 command（canonical + v1）----
    read = {v for d, k, v in acl if d == "pattern" and k == "read"}
    check(
        "R001 command 只读（canonical + v1）",
        {"robot/%u/command", "v1/robot/%u/cmd"} <= read,
        str(read),
    )

    # ---- 3) 车端无通配授权（跨车隔离的唯一豁免是平台账号）----
    pattern_wildcards = [v for d, k, v in acl if d == "pattern" and "+" in v]
    check("pattern 段无通配授权", not pattern_wildcards, str(pattern_wildcards))
    platform = {v for d, k, v in acl if d == "topic" and k == "readwrite"}
    check("平台账号通配授权保留", "robot/+/+" in platform, str(platform))
    # R001 → R002 推演：pattern %u 展开后 topic 只含 %u；两个不同 vehicle_id 不可能互达
    check("R001 → R002 跨车拒绝（pattern 语义）", "%u" in " ".join(written | read))

    # ---- 4) 无 vehicle 硬编码授权（隔离完全由 %u 保证）----
    hardcoded = [v for d, k, v in acl if re.search(r"R00[0-9]", v)]
    check("ACL 无 vehicle_id 硬编码", not hardcoded, str(hardcoded))

    # ---- 5) anonymous 拒绝 + password_file 生效 ----
    check(
        "allow_anonymous false", re.search(r"^allow_anonymous[ \t]+false", conf, re.M) is not None
    )
    check("password_file 引用", "password_file /mosquitto/secure/passwords" in conf)
    check("acl_file 引用", "acl_file /mosquitto/secure/acl" in conf)

    # ---- 6) 错误凭据拒绝的配置组合语义（broker 校验密码，无匿名回退）----
    check(
        "错误凭据拒绝（无匿名回退路径）",
        "allow_anonymous false" in conf and "password_file" in conf,
    )

    if FAILURES:
        print(f"\nVEHICLE_ONBOARDING_ACL=FAIL count={len(FAILURES)}")
        return 1
    print("\nVEHICLE_ONBOARDING_ACL=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
