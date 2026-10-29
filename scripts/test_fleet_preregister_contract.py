#!/usr/bin/env python3
"""Fleet preregister + 只读门 静态合同测试（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE / P0-B）。

锁定生产语义（静态断言，CI 可跑）：
  - fleet sites/preregister：受控实现接线、显式 --site-code、
    无 credential/token/downlink/allowlist 引用、不触碰 R002 测试账号、
    不调用 60-onboard（职责分离）
  - vehicle verify：is-active 门（enabled/active/activating/reloading
    → READONLY_SAFETY_GATE=FAIL）
  - vehicle install：firebot-control active → CONTROL_INSTALL=REJECTED /
    EXISTING_CONTROL_ACTIVE（不自动 stop）
  - readonly-preflight：bootstrap/enrolled 两阶段、
    SUPPORTED_COMMANDS_NOT_EMPTY、无 MQTT publish
  - readonly-start：唯一启动点 enable --now firebot-vehicle-edge；
    可执行行不触碰 control/roscore/move_base/cmd_vel
  - P0-G：正式入口统一 firebotctl（底层 systemctl 提示标 DEVELOPER/DEBUG ONLY）
  - 服务层 pytest 覆盖存在性（CREATE/IDEMPOTENT/CONFLICT/NO_SECRET/OTHER_UNTOUCHED）

用法：python3 scripts/test_fleet_preregister_contract.py
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
    """可执行行（排除注释）——注释中的“禁止/绝不”负向声明不是生产语义。"""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_fleet_preregister_surface() -> None:
    ops = read("apps/api/app/modules/fleet/ops.py")
    check(
        "PREREGISTER 受控服务层存在（SQLAlchemy model）",
        "class PreregisterResult" in ops and "def preregister" in ops,
    )
    check("SITE_NOT_FOUND 语义", "SITE_NOT_FOUND" in ops)
    check("SITE_INACTIVE 语义", "SITE_INACTIVE" in ops)
    check("幂等 EXISTS 语义", '"EXISTS"' in ops and "IDENTICAL" in ops)
    check(
        "冲突 SITE_MISMATCH/NAME_MISMATCH 不自动更新",
        "SITE_MISMATCH" in ops and "NAME_MISMATCH" in ops,
    )
    check("新 Robot enabled=False 只读基线", "enabled=False" in ops)
    check(
        "assignment 基线 supported_commands_json=[] location_enabled=False",
        "supported_commands_json=[]" in ops and "location_enabled=False" in ops,
    )
    # 服务层绝不触碰 credential/token/downlink/allowlist（只检查函数体；
    # docstring 中的“绝不”负向声明是合法语义）
    body = ops.split("def preregister", 1)[1].split("\ndef ", 1)[0]
    check(
        "FLEET_PREREGISTER_NO_SECRET（preregister 函数体无 credential/token/downlink/allowlist）",
        not any(
            re.search(pat, body)
            for pat in (
                "mqtt_password",
                "issue_token",
                "RobotCredential",
                "EnrollmentToken",
                "DOWNLINK",
                "ALLOWLIST",
                "SRT",
            )
        ),
    )
    check("R002_TEST_ACCOUNT_UNCHANGED（ops.py 不引用 R002）", "R002" not in ops)

    sh = read("integration/ros1/fleet-preregister.sh")
    check("preregister 显式 --site-code 必填", "--site-code" in sh and "禁止随机选 Site" in sh)
    check("preregister 不调用 60-onboard（职责分离）", "60-onboard" not in sh)
    check(
        "preregister 不生成 token（无 openssl rand/issue_token）",
        "openssl" not in sh and "issue_token" not in sh,
    )
    check(
        "DOWNLINK_UNCHANGED（preregister 不引用 DOWNLINK/ALLOWLIST）",
        "DOWNLINK" not in sh and "ALLOWLIST" not in sh,
    )
    check("R002_TEST_ACCOUNT_UNCHANGED（preregister 不引用 R002）", "R002" not in sh)

    sites_sh = read("integration/ros1/fleet-sites.sh")
    check("fleet sites 只读（不调用 60-onboard）", "60-onboard" not in sites_sh)
    check(
        "fleet sites 输出字段 SITE_CODE/SITE_NAME/ACTIVE/ROBOT_COUNT",
        "SITE_CODE" in sites_sh and "ROBOT_COUNT" in sites_sh,
    )


def test_firebotctl_wiring() -> None:
    ctl = read("integration/ros1/firebotctl")
    check("fleet sites 子命令接线", "sites)\n" in ctl and "fleet-sites.sh" in ctl)
    check("fleet preregister 子命令接线", "preregister)" in ctl and "fleet-preregister.sh" in ctl)
    check(
        "readonly-preflight 子命令接线",
        "readonly-preflight)" in ctl and "readonly-preflight.sh" in ctl,
    )
    check("readonly-start 子命令接线", "readonly-start)" in ctl and "readonly-start.sh" in ctl)
    # P0-C：verify 必须同时检查 enabled + active（经 systemd_state.sh 规范化，stdout/exit 分离）
    check(
        "VERIFY_CONTROL_ACTIVE_GATE（unit_enabled_state + unit_active_state 双检查）",
        "unit_enabled_state firebot-control" in ctl and "unit_active_state firebot-control" in ctl,
    )
    check("verify active|activating|reloading 门", "active|activating|reloading" in ctl)
    check("verify READONLY_SAFETY_GATE=FAIL 语义", "READONLY_SAFETY_GATE=FAIL" in ctl)
    check("verify 输出 CONTROL_ACTIVE", "CONTROL_ACTIVE=" in ctl)


def test_install_control_active_gate() -> None:
    sh = read("integration/ros1/vehicle-install.sh")
    check("CONTROL_INSTALL=REJECTED 语义", "CONTROL_INSTALL=REJECTED" in sh)
    check("EXISTING_CONTROL_ACTIVE 语义", "EXISTING_CONTROL_ACTIVE" in sh)
    check("install 不自动 stop control（由人工确认）", "由人工确认" in sh)
    active = active_lines(sh)
    check(
        "install active 门覆盖 active|activating|reloading",
        any("active|activating|reloading" in ln for ln in active),
    )
    check(
        "install 不自动 stop firebot-control（可执行行无 systemctl stop firebot-control）",
        not any("stop firebot-control" in ln for ln in active),
    )


def test_readonly_scripts_surface() -> None:
    # 生产逻辑唯一 source of truth = vehicle-edge/lifecycle/（旧路径是 thin shim）
    pre = read("integration/ros1/vehicle-edge/lifecycle/readonly-preflight.sh")
    check("READONLY_BOOTSTRAP_PREFLIGHT 输出存在", "READONLY_BOOTSTRAP_PREFLIGHT=" in pre)
    check("READONLY_ENROLLED_PREFLIGHT 输出存在", "READONLY_ENROLLED_PREFLIGHT=" in pre)
    check("bootstrap 检查 HTTPS 系统 CA（无 -k/--cacert）", "HTTPS_SYSTEM_CA" in pre)
    check("MQTT 仅 TLS 握手（不得 MQTT publish）", "s_client" in pre)
    check(
        "preflight 无 MQTT publish 工具引用（mosquitto_pub 不在）",
        "mosquitto_pub" not in pre,
    )
    check("SUPPORTED_COMMANDS_NOT_EMPTY 语义", "SUPPORTED_COMMANDS_NOT_EMPTY" in pre)
    check(
        "Control enabled/active 门（经 systemd_state.sh）",
        "unit_enabled_state firebot-control" in pre and "unit_active_state firebot-control" in pre,
    )
    check("NTP 检查", "NTPSynchronized" in pre)

    start = read("integration/ros1/vehicle-edge/lifecycle/readonly-start.sh")
    check("READONLY_BRIDGE_START 输出存在", "READONLY_BRIDGE_START=" in start)
    check(
        "readonly-start 唯一启动点 enable --now firebot-vehicle-edge",
        "enable --now firebot-vehicle-edge" in start,
    )
    start_active = active_lines(start)
    check(
        "READONLY_START 不触碰 control/roscore/base/navigation/move_base/cmd_vel（可执行行）",
        not any(
            term in ln
            for ln in start_active
            for term in ("roscore", "move_base", "cmd_vel", "roslaunch", "navigation", "base ")
        ),
    )
    check(
        "READONLY_START 不启动 firebot-control（可执行行无 start/enable firebot-control）",
        not any(re.search(r"(enable|start)\s+firebot-control", ln) for ln in start_active),
    )


def test_p0g_unified_entry() -> None:
    ctl = read("integration/ros1/firebotctl")
    check(
        "P0-G 正式入口统一（help 含 readonly-preflight/readonly-start）",
        "readonly-preflight" in ctl and "readonly-start" in ctl,
    )
    install_sh = read("integration/ros1/vehicle-bridge/install.sh")
    check("P0-G 底层 systemctl 提示标 DEVELOPER/DEBUG ONLY", "DEVELOPER/DEBUG ONLY" in install_sh)


def test_pytest_coverage_exists() -> None:
    t = read("apps/api/tests/test_fleet_preregister.py")
    for name in (
        "test_first_create_baseline",
        "test_second_identical_idempotent",
        "test_site_conflict_rejected",
        "test_identity_conflict_rejected",
        "test_no_credential_and_no_token_created",
        "test_unknown_site_rejected",
        "test_inactive_site_rejected",
        "test_other_robot_untouched",
    ):
        check(f"FLEET_PREREGISTER pytest 存在：{name}", f"def {name}" in t)
    gate = read("integration/ros1/vehicle-bridge/tests/test_readonly_gate.py")
    for name in (
        "test_readonly_bootstrap_preflight_pass",
        "test_readonly_bootstrap_active_but_disabled_control_rejected",
        "test_readonly_enrolled_preflight_pass",
        "test_readonly_enrolled_supported_commands_not_empty_rejected",
        "test_readonly_start_bridge_only",
        "test_readonly_start_rejected_control_active",
    ):
        check(f"READONLY_GATE pytest 存在：{name}", f"def {name}" in gate)


def main() -> int:
    test_fleet_preregister_surface()
    test_firebotctl_wiring()
    test_install_control_active_gate()
    test_readonly_scripts_surface()
    test_p0g_unified_entry()
    test_pytest_coverage_exists()
    if FAILURES:
        print(f"\nFLEET_PREREGISTER_CONTRACT_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nFLEET_PREREGISTER_CONTRACT_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
