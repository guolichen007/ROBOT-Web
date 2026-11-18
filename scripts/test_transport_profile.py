#!/usr/bin/env python3
"""G4-Fleet-R2：Transport Profile 解析 + fleet-register 行为测试（fake docker；CI 可跑）。

覆盖：
  REGISTRY_INVARIANTS     Profile 字段齐全；provisioner.broker_ref == broker_ref；
                          001→r001-independent-v1(8884, 无 provisioner)；
                          002→u-dao-standard-v1(8883) 不变
  RESOLVE_NEGATIVE        PROFILE_REQUIRED / PROFILE_CONFLICT / PROFILE_NOT_ASSIGNABLE /
                          HOST_INVALID / NO_PROVISIONER / BROKER_MISMATCH（篡改注册表）
  FLEET_REGISTER_001      001 → REJECTED：不调用 provisioner（MQTT 不生成/不轮换）、不签发 token
  FLEET_REGISTER_NEW      新车无 --transport-profile → REJECTED；显式 u-dao-standard-v1 → PASS：
                          provisioner --apply + --media-only；payload 经 stdin，
                          含 Profile 参数 + media；
                          token 只写 0600 文件且不出现在 stdout；容器 argv 无凭证
  FLEET_REGISTER_002      002 改用 r001-independent-v1 → REJECTED（禁止影响 R002）
用法：python3 scripts/test_transport_profile.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "u-dao" / "scripts"))
import transport_profile as tp  # noqa: E402

FAILURES: list[str] = []
REGISTRY = ROOT / "deploy" / "u-dao" / "transport-profiles.json"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)[:400]))
    if not cond:
        FAILURES.append(name)


def expect(code: str, **kw) -> None:
    try:
        tp.resolve(**kw)
    except tp.ProfileError as exc:
        check(f"RESOLVE_NEGATIVE {code}", exc.code == code, exc.code)
        return
    check(f"RESOLVE_NEGATIVE {code}", False, "no error")


def test_registry_and_resolve(act: Path) -> None:
    data = tp.load_registry(REGISTRY)
    for pid, prof in data["profiles"].items():
        prov = prof.get("credential_provisioner")
        check(
            f"REGISTRY_INVARIANTS {pid} 字段齐全且 provisioner 指向同一 Broker",
            all(prof.get(k) not in (None, "") for k in tp.REQUIRED)
            and (prov is None or prov["broker_ref"] == prof["broker_ref"]),
        )
    r1 = tp.resolve("firebot-vehicle-01", activation_env=act)
    check(
        "001 保持 r001-independent-v1:8884、vehicle-agent-v1、无 provisioner",
        r1["transport_profile"] == "r001-independent-v1"
        and r1["mqtt_port"] == 8884
        and r1["protocol"] == "vehicle-agent-v1"
        and r1["credential_provisioner"] is None,
        r1,
    )
    r2 = tp.resolve("firebot-vehicle-02", activation_env=act, require_provisioner=True)
    check(
        "002 保持 u-dao-standard-v1:8883 主 Broker（R002 不受影响）",
        r2["transport_profile"] == "u-dao-standard-v1"
        and r2["mqtt_port"] == 8883
        and r2["broker_ref"] == "u-dao-firebot/mosquitto"
        and r2["mqtt_host"] == "firebot.youdaozd.com",
        r2,
    )
    r3 = tp.resolve("firebot-vehicle-03", profile="u-dao-standard-v1", activation_env=act)
    check(
        "新车显式标准 Profile 解析",
        r3["transport_profile"] == "u-dao-standard-v1" and not r3["mapped"],
    )
    expect("PROFILE_REQUIRED", vehicle_id="firebot-vehicle-03", activation_env=act)
    expect(
        "PROFILE_CONFLICT",
        vehicle_id="firebot-vehicle-02",
        profile="r001-independent-v1",
        activation_env=act,
    )
    expect(
        "PROFILE_NOT_ASSIGNABLE",
        vehicle_id="firebot-vehicle-03",
        profile="r001-independent-v1",
        activation_env=act,
    )
    expect(
        "NO_PROVISIONER",
        vehicle_id="firebot-vehicle-01",
        activation_env=act,
        require_provisioner=True,
    )
    bad = act.parent / "bad.env"
    bad.write_text("MQTT_PUBLIC_HOSTNAME=100.110.31.112\n")
    expect("HOST_INVALID", vehicle_id="firebot-vehicle-02", activation_env=bad)
    expect("HOST_INVALID", vehicle_id="firebot-vehicle-02", activation_env=act.parent / "missing")
    tampered = json.loads(REGISTRY.read_text())
    prov = tampered["profiles"]["u-dao-standard-v1"]["credential_provisioner"]
    prov["broker_ref"] = "other/broker"
    tf = act.parent / "tampered.json"
    tf.write_text(json.dumps(tampered))
    expect("BROKER_MISMATCH", vehicle_id="firebot-vehicle-02", registry=tf, activation_env=act)


FAKE_DOCKER = r"""#!/bin/bash
# fake docker：记录 argv；compose exec api python 按 stdin 内容模拟 Robot 查询 / token 签发
L="$FAKE_LOG"; printf 'ARGV %s\n' "$*" >> "$L"
case "$*" in
  *"ps -q"*) echo cid-mosq; exit 0 ;;
  *"inspect"*) echo 2026-11-18T00:00:00Z; exit 0 ;;
  *"exec -T api python"*)
     in="$(cat)"; printf 'STDIN %s\n' "$in" >> "$L"
     case "$in" in
       firebot-vehicle-0[1-3]) echo YES ;;
       \{*) printf '%s' "$in" > "$FAKE_PAYLOAD"; echo "f00d$(printf %060d 0)" ;;
       *) echo NO ;;
     esac; exit 0 ;;
esac
exit 0
"""

FAKE_ONBOARD = r"""#!/bin/bash
# fake provisioner：记录调用；--apply 生成 mqtt_password，--media-only 生成媒体副本
echo "ONBOARD $*" >> "$FAKE_LOG"
d="$U_DAO_RUNTIME_ROOT/secrets/vehicles/$1"; mkdir -p "$d"; umask 077
case "$2" in
  --apply) [ -f "$d/mqtt_password" ] || printf 'mqtt-pw-%s' "$1" > "$d/mqtt_password" ;;
  --media-only)
    printf '%064d' 7 > "$d/srt_publish_token"
    printf 'shared-srt-pass' > "$d/srt_publish_passphrase" ;;
esac
"""


class FleetFx:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-tp-"))
        self.repo = self.tmp / "repo"
        for rel in (
            "integration/ros1/fleet-register.sh",
            "scripts/docker_cmd.sh",
            "deploy/u-dao/transport-profiles.json",
            "deploy/u-dao/scripts/transport_profile.py",
        ):
            dst = self.repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, dst)
        onboard = self.repo / "deploy/u-dao/scripts/60-onboard-vehicle.sh"
        onboard.write_text(FAKE_ONBOARD)
        self.runtime = self.tmp / "runtime"
        (self.runtime / "env").mkdir(parents=True)
        (self.runtime / "env/public-activation.env").write_text(
            "MQTT_PUBLIC_HOSTNAME=firebot.youdaozd.com\nPUBLIC_DOMAIN=firebot.youdaozd.com\n"
            "MEDIA_SRT_PUBLIC_PORT=8890\n"
        )
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.payload = self.tmp / "payload.json"

    def run(self, *args):
        env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            U_DAO_RUNTIME_ROOT=str(self.runtime),
            U_DAO_REPO_ROOT=str(self.repo),
            DOCKER_CMD_SKIP_DETECT="1",
            FAKE_LOG=str(self.log),
            FAKE_PAYLOAD=str(self.payload),
        )
        return subprocess.run(
            ["bash", str(self.repo / "integration/ros1/fleet-register.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


def test_fleet_register() -> None:
    fx = FleetFx()
    try:
        r = fx.run("firebot-vehicle-01")
        check(
            "FLEET_REGISTER_001 → REJECTED（NO_PROVISIONER）",
            r.returncode != 0 and "NO_PROVISIONER" in r.stderr and "REJECTED" in r.stderr,
            r.stdout + r.stderr,
        )
        check(
            "FLEET_REGISTER_001 未调用 provisioner、未签发 token、未生成 MQTT 密码",
            "ONBOARD" not in fx.calls()
            and not fx.payload.exists()
            and not (fx.runtime / "secrets/vehicles/firebot-vehicle-01").exists(),
        )
        r = fx.run("firebot-vehicle-03")
        check(
            "FLEET_REGISTER_NEW 无 --transport-profile → REJECTED",
            r.returncode != 0 and "PROFILE_REQUIRED" in r.stderr and "ONBOARD" not in fx.calls(),
        )
        r = fx.run("firebot-vehicle-02", "--transport-profile", "r001-independent-v1")
        check(
            "FLEET_REGISTER_002 改用他 Profile → REJECTED",
            r.returncode != 0 and "PROFILE_CONFLICT" in r.stderr,
        )
        r = fx.run("firebot-vehicle-03", "--transport-profile", "u-dao-standard-v1")
        check("FLEET_REGISTER_NEW 显式标准 Profile → PASS", r.returncode == 0, r.stdout + r.stderr)
        calls = fx.calls()
        check(
            "provisioner = Profile 指定（--apply + --media-only）",
            "ONBOARD firebot-vehicle-03 --apply" in calls
            and "ONBOARD firebot-vehicle-03 --media-only" in calls,
        )
        payload = json.loads(fx.payload.read_text())
        check(
            "payload 含 Profile 参数（host/port/protocol/broker_ref/ca）",
            payload["transport_profile"] == "u-dao-standard-v1"
            and payload["mqtt_port"] == 8883
            and payload["protocol"] == "canonical-mqtt-1.3"
            and payload["broker_ref"] == "u-dao-firebot/mosquitto"
            and payload["ca_vehicle_path"] == "certs/mqtt-ca.crt",
            payload,
        )
        check(
            "payload 媒体凭证（单车 token + endpoint；无主密钥）",
            payload["media"]["vehicle_id"] == "firebot-vehicle-03"
            and payload["media"]["srt_port"] == 8890
            and "media_publish_token" not in json.dumps(payload),
        )
        argv_lines = [ln for ln in calls.splitlines() if ln.startswith("ARGV")]
        check(
            "容器/docker argv 无凭证（mqtt 密码 / SRT token / device token）",
            not any(
                s in ln
                for ln in argv_lines
                for s in ("mqtt-pw-", "0" * 63 + "7", payload["device_token"], "shared-srt-pass")
            ),
        )
        tok = fx.runtime / "secrets/vehicles/firebot-vehicle-03/enroll-token"
        check(
            "ENROLL_TOKEN 只写 0600 文件且不回显",
            tok.is_file()
            and stat.S_IMODE(tok.stat().st_mode) == 0o600
            and tok.read_text().strip() not in r.stdout
            and "ENROLL_TOKEN_FILE=" in r.stdout,
            (tok.is_file() and oct(stat.S_IMODE(tok.stat().st_mode)), r.stdout[-300:]),
        )
        check("无残留临时 token 文件", not list(tok.parent.glob(".enroll-token*")))
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        act = Path(t) / "public-activation.env"
        act.write_text("MQTT_PUBLIC_HOSTNAME=firebot.youdaozd.com\n")
        test_registry_and_resolve(act)
    test_fleet_register()
    if FAILURES:
        print(f"\nTRANSPORT_PROFILE_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nTRANSPORT_PROFILE_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
