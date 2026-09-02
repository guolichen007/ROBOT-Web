"""Mosquitto ACL 模板静态校验：车辆 %u 规则必须用 pattern，且不能写 command。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ACL = ROOT / "infra" / "mosquitto" / "acl.example"

VEHICLE_WRITE_TOPICS = {
    "availability",
    "heartbeat",
    "capabilities",
    "status",
    "sensor",
    "location",
    "alarm",
    "task_status",
    "command_ack",
}


def _vehicle_blocks() -> list[dict]:
    blocks: list[dict] = []
    current: dict | None = None
    for raw in ACL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("user "):
            if current:
                blocks.append(current)
            current = {"user": line.split("user ", 1)[1].strip(), "rules": []}
        elif current is not None:
            current["rules"].append(line)
    if current:
        blocks.append(current)
    return [b for b in blocks if b["user"] != "platform"]


def test_vehicle_acl_uses_pattern_not_topic() -> None:
    for block in _vehicle_blocks():
        for rule in block["rules"]:
            assert not rule.startswith("topic "), f"{block['user']} 使用 topic 而非 pattern: {rule}"
            assert rule.startswith("pattern "), f"{block['user']} 缺少 pattern 关键字: {rule}"


def test_vehicle_can_write_telemetry_and_read_own_command() -> None:
    for block in _vehicle_blocks():
        user = block["user"]
        write = {
            r.split("robot/%u/", 1)[1]
            for r in block["rules"]
            if r.startswith("pattern write robot/%u/")
        }
        assert write == VEHICLE_WRITE_TOPICS, f"{user} 写权限集合不完整/不正确: {write}"
        assert "pattern read robot/%u/command" in block["rules"], f"{user} 缺 command 读权限"


def test_vehicle_cannot_write_command() -> None:
    for block in _vehicle_blocks():
        for rule in block["rules"]:
            assert not ("write" in rule and rule.endswith("/command")), (
                f"{block['user']} 非法拥有 command 写权限: {rule}"
            )
