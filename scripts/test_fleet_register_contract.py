#!/usr/bin/env python3
"""REAL_VEHICLE_READONLY_PREP 生产链路合同测试（静态断言，CI 可跑）。

锁定 U-Dao 实车注册与车端 TLS 信任链的生产语义（P0-1..P0-7 / P1）：
  - fleet register：public-activation.env 权威 host、canonical onboarding、零重启、不旋转、
    secret 输出扫描（输出行不含 credential 变量）
  - 车端 HTTPS 三路径系统 CA（无 --cacert / -k）；MQTT 信任锚 /etc/firebot/mqtt-ca.crt
  - 无 Tailscale/旧 IP 默认（100.110./100.68./100.64./production-ca 生产语义残留）
  - enrollment 四态测试（first/replay/wrong-device/expired）由 backend pytest 锁定（存在性断言）
  - 只读铁律：Bridge 源码不发布 /cmd_vel；SUPPORTED_COMMANDS 默认为空；CONTROL_ENABLED=NO
  - media-secret 落盘规范入口存在
用法：python3 scripts/test_fleet_register_contract.py
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


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def active_lines(text: str) -> list[str]:
    """可执行行（排除注释行）——注释中的「禁止/不再」负向声明不是生产语义。"""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_fleet_register_host_source() -> None:
    script = read("integration/ros1/fleet-register.sh")
    check("U_DAO_FLEET_REGISTER_HOST 读取 public-activation.env", "public-activation.env" in script)
    check(
        "U_DAO_FLEET_REGISTER_HOST=firebot.youdaozd.com 语义（MQTT_PUBLIC_HOSTNAME 优先）",
        "MQTT_PUBLIC_HOSTNAME" in script and "PUBLIC_DOMAIN" in script,
    )
    check("U_DAO_FLEET_REGISTER_PORT 默认 8883", "U_DAO_FLEET_REGISTER_PORT:-8883" in script)
    # 可执行行不得含旧 IP/Tailscale 默认值（校验用的拒绝正则与注释声明除外）
    check(
        "NO_LEGACY_IP_DEFAULT（无 100.110/100.68/100.64 默认值）",
        not any(
            re.search(r":-\s*100\.(110|68|64)\.|\"100\.(110|68|64)\.", ln)
            for ln in active_lines(script)
        ),
    )
    check(
        "NO_TAILSCALE_DEFAULT（无 Tailscale 正向使用语义）",
        not any(re.search(r"Tailscale 登录|经 Tailscale", ln) for ln in active_lines(script)),
    )


def test_fleet_register_canonical_onboarding() -> None:
    script = read("integration/ros1/fleet-register.sh")
    check(
        "复用 60-onboard-vehicle.sh --apply（canonical onboarding）",
        "60-onboard-vehicle.sh" in script and "--apply" in script,
    )
    # 可执行行断言：旧自管 credential 动作不得再出现（注释中的“不再”声明除外）
    check(
        "不再自建 mosquitto_passwd",
        not any("mosquitto_passwd" in ln for ln in active_lines(script)),
    )
    check(
        "不再 restart mosquitto",
        not any(re.search(r"restart\s+mosquitto", ln) for ln in active_lines(script)),
    )
    check("不再写旧 ./secrets", not any("./secrets" in ln for ln in active_lines(script)))
    check("不再从 compose port 推断 MQTT_PORT", " port mosquitto" not in script)
    check(
        "MOSQUITTO_ZERO_RESTART 断言（StartedAt 前后一致）",
        "StartedAt" in script and "MOSQUITTO_ZERO_RESTART" in script,
    )
    check(
        "已存在凭据不旋转断言（sha256 前后一致 + R001_MQTT_CREDENTIAL_ROTATED=NO）",
        "R001_MQTT_CREDENTIAL_ROTATED=NO" in script and "_EXISTING_CREDENTIAL_REUSED" in script,
    )


def test_fleet_register_secret_output_scan() -> None:
    """静态扫描：fleet-register 的输出语句不得打印 credential 变量值。"""
    script = read("integration/ros1/fleet-register.sh")
    # 提取 echo/printf 行（含多行续行简化处理：单行 echo 为主）
    output_lines = [ln for ln in script.splitlines() if re.match(r"\s*(echo|printf)", ln)]
    leaked = []
    for ln in output_lines:
        if re.search(r"\$PASSWORD|\$DEVICE_TOKEN|\$SRT_TOKEN|\$PASSPHRASE", ln):
            leaked.append(ln.strip()[:80])
    check(
        "SECRET_OUTPUT_SCAN（输出行不含 MQTT password/device token/SRT 凭据）",
        not leaked,
        "; ".join(leaked),
    )
    check("只输出 ENROLL_TOKEN 一次性交付", "ENROLL_TOKEN=$ENROLL_TOKEN" in script)
    check(
        "MQTT credential 只进 token payload（不回显）",
        "\"'$PASSWORD'\"" in script or "'$PASSWORD'" in script,
    )


def test_https_system_ca() -> None:
    # enroll 实现唯一 source of truth = vehicle-edge/lifecycle/enroll.sh（firebotctl 转发）
    firebotctl = (
        read("integration/ros1/firebotctl")
        + "\n"
        + read("integration/ros1/vehicle-edge/lifecycle/enroll.sh")
    )
    # 实际 curl 命令行不得携带 --cacert / -k（注释中的“禁止”声明是合法负向语义）
    curl_lines = [ln for ln in firebotctl.splitlines() if re.match(r"\s*curl ", ln)]
    check(
        "HTTPS_SYSTEM_CA（curl 命令行无 --cacert/-k）",
        not any("--cacert" in ln or re.search(r"(^|\s)-k(\s|$)", ln) for ln in curl_lines),
        "; ".join(ln.strip()[:60] for ln in curl_lines if "--cacert" in ln or " -k " in ln),
    )
    check(
        "enroll 强制 https://（禁止 http）",
        "https://*) : ;;" in firebotctl or "必须为 https://" in firebotctl,
    )
    check(
        "enroll 拒绝 IP/Tailscale hostname",
        "100\\.(64|68|110)" in firebotctl and "tailscale" in firebotctl.lower(),
    )
    check("系统 CA 语义注释存在（禁止 --cacert / -k）", "禁止 --cacert / -k" in firebotctl)


def test_mqtt_private_ca_path() -> None:
    check(
        "MQTT_PRIVATE_CA_PATH（bridge config 默认 /etc/firebot/mqtt-ca.crt）",
        '"/etc/firebot/mqtt-ca.crt"'
        in read("integration/ros1/vehicle-bridge/firebot_bridge/config.py"),
    )
    check(
        "MQTT_PRIVATE_CA_PATH（generate_bridge_env 写入 mqtt-ca.crt）",
        "FIREBOT_CA_CERT=/etc/firebot/mqtt-ca.crt" in read("integration/ros1/firebotctl"),
    )
    check(
        "MQTT_PRIVATE_CA_PATH（enrollment API 默认 ca_cert=mqtt-ca.crt）",
        '"/etc/firebot/mqtt-ca.crt"' in read("apps/api/app/modules/enrollment/router.py"),
    )
    check(
        "MQTT_PRIVATE_CA_PATH（fleet-register 签发 ca_cert=mqtt-ca.crt）",
        "'/etc/firebot/mqtt-ca.crt'" in read("integration/ros1/fleet-register.sh"),
    )
    cfg = read("integration/ros1/vehicle-bridge/firebot_bridge/config.py")
    check(
        "bridge config 无 MQTT host IP 默认 + validate fail-closed",
        'os.environ.get("FIREBOT_MQTT_HOST", "")' in cfg and "FIREBOT_MQTT_HOST 未设置" in cfg,
    )
    check(
        "bridge.env.example 使用 mqtt-ca.crt",
        "FIREBOT_CA_CERT=/etc/firebot/mqtt-ca.crt"
        in read("integration/ros1/vehicle-bridge/config/bridge.env.example"),
    )


def test_no_tailscale_production_reference() -> None:
    # 生产接入路径不得残留「Tailscale/旧 IP 默认值」或 production-ca；
    # 注释/校验中的“禁止/历史方案(NOT FOR PRODUCTION)”负向声明是合法语义，不判定为残留。
    files = [
        "integration/ros1/fleet-register.sh",
        "integration/ros1/firebotctl",
        "integration/ros1/vehicle-bridge/firebot_bridge/config.py",
        "integration/ros1/vehicle-bridge/config/bridge.env.example",
        "docs/部署运维/车端部署与实车接口.md",
        "docs/部署运维/R001现场接入检查清单.md",
        "docs/技术合同/FIREBOT_BRIDGE_CONTRACT_1.3.md",
    ]
    hits: list[str] = []
    for f in files:
        for i, ln in enumerate(read(f).splitlines(), 1):
            # 1) 旧 IP/Tailscale 作为默认值（:-, ="…, FIREBOT_MQTT_HOST=100.）
            if re.search(
                r"(?::-)100\.(110|68|64)\.|FIREBOT_MQTT_HOST=\"?100\.|FIREBOT_MQTT_HOST=100\.", ln
            ):
                hits.append(f"{f}:{i}:legacy-default")
            # 2) production-ca 任何形态（生产路径必须使用 mqtt-ca.crt）
            if "production-ca" in ln:
                hits.append(f"{f}:{i}:production-ca")
            # 3) 正向指引使用 Tailscale（“Tailscale 登录/经 Tailscale 访问”等）
            if re.search(r"Tailscale\s*(登录|访问|直连)|经\s*Tailscale", ln, re.IGNORECASE):
                hits.append(f"{f}:{i}:tailscale-usage")
    check(
        "TAILSCALE_PRODUCTION_REFERENCE=NONE + LEGACY_100_NET_DEFAULT=NONE（生产路径）",
        not hits,
        "; ".join(hits),
    )


def test_enrollment_gates_coverage() -> None:
    t = read("apps/api/tests/test_enrollment_token.py")
    for name in (
        "test_token_single_use_and_replay_rejected",
        "test_token_bound_to_device_id",
        "test_token_expiry_rejected",
        "test_wrong_token_rejected",
    ):
        check(f"ENROLL gate 测试存在：{name}", f"def {name}" in t)


def test_control_default_off() -> None:
    bridge_src = ROOT / "integration/ros1/vehicle-bridge/firebot_bridge"
    cmd_vel_refs: list[str] = []
    for f in sorted(bridge_src.rglob("*.py")):
        for i, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if "cmd_vel" in ln and "rostopic" not in ln:
                cmd_vel_refs.append(f"{f.name}:{i}")
    check(
        "CONTROL_DEFAULT_OFF（Bridge 源码无 /cmd_vel 发布引用）",
        not cmd_vel_refs,
        "; ".join(cmd_vel_refs[:3]),
    )
    firebotctl = read("integration/ros1/firebotctl")
    check(
        "CONTROL_DEFAULT_OFF（SUPPORTED_COMMANDS 默认为空）",
        'assignment.get("SUPPORTED_COMMANDS", "")' in firebotctl,
    )
    check(
        "CONTROL_DEFAULT_OFF（install 输出 CONTROL_ENABLED=NO）",
        'echo "CONTROL_ENABLED=NO"' in firebotctl,
    )


def test_media_handoff_contract() -> None:
    firebotctl = read("integration/ros1/firebotctl")
    check(
        "MEDIA_HANDOFF（media-secret check 子命令存在）",
        "media-secret" in firebotctl and "check_media_secret" in firebotctl,
    )
    doc = read("docs/部署运维/车端部署与实车接口.md")
    check(
        "MEDIA_HANDOFF（media-secret.env 0600 root:root 规范入文档）",
        "/etc/firebot/media-secret.env" in doc and "0600 root:root" in doc,
    )
    check(
        "MEDIA_HANDOFF（视频与 ROS Bridge 分开 + 基线 not_available）",
        "roof_rgb=not_available" in doc and "roof_thermal=not_available" in doc,
    )
    check(
        "MEDIA_HANDOFF（SRT 公网入口 firebot.youdaozd.com:8890 入文档）",
        "firebot.youdaozd.com:8890" in doc,
    )


def main() -> int:
    test_fleet_register_host_source()
    test_fleet_register_canonical_onboarding()
    test_fleet_register_secret_output_scan()
    test_https_system_ca()
    test_mqtt_private_ca_path()
    test_no_tailscale_production_reference()
    test_enrollment_gates_coverage()
    test_control_default_off()
    test_media_handoff_contract()
    if FAILURES:
        print(f"\nFLEET_REGISTER_CONTRACT_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nFLEET_REGISTER_CONTRACT_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
