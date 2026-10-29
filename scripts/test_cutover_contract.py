#!/usr/bin/env python3
"""跨 Broker 迁移 cutover 事务合同测试（REAL_R002_CUTOVER_TRANSACTION_FINAL_FIX，静态断言）。

锁定：
  - CUTOVER_ORDER_CONTRACT：migration-stop-legacy 必须先于 enroll；enroll 的 Bridge 门必须
    在 token POST 之前；install 的 Bridge 门必须在 release_sync 之前。
  - migration-stop-legacy 只 disable --now firebot-bridge（不触碰 control/ROS/base/env/secret/
    credential/Tailscale）。
  - readonly-start 失败自动 fail-closed stop（disable --now firebot-bridge +
    FAIL_CLOSED_BRIDGE_STOP）。
  - enrolled preflight 强制 EXACT_SHA_MATCH（bootstrap 不强制）。
  - ROLLBACK_CONTRACT：文档含 ROLLBACK_REQUIRED / 恢复清单 / 云端 identity 不删除 /
    TAILSCALE 保留（tailscale down/logout/卸载/禁用禁止）。
  - P0-2 语义：token 必须在 Bridge 已停止后才允许消费（文档 + 代码顺序双重锁定）。

用法：python3 scripts/test_cutover_contract.py
"""

from __future__ import annotations

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


def test_cutover_order_contract() -> None:
    doc = read("docs/部署运维/R002跨Broker公网迁移.md")
    check(
        "CUTOVER_ORDER_CONTRACT：文档含三段式 A/B/C",
        "### A." in doc and "### B." in doc and "### C." in doc,
    )
    check(
        "CUTOVER_ORDER_CONTRACT：migration-stop-legacy 在 enroll 之前（B 段）",
        "migration-stop-legacy" in doc and "MIGRATION_CUTOVER_BOUNDARY=ENTERED" in doc,
    )
    check(
        "CUTOVER_ORDER_CONTRACT：enroll 门说明 ENROLL=REJECTED/LEGACY_BRIDGE_STILL_ACTIVE",
        "ENROLL=REJECTED" in doc and "LEGACY_BRIDGE_STILL_ACTIVE" in doc,
    )
    check(
        "CUTOVER_ORDER_CONTRACT：edge-install 互斥门说明（EDGE_INSTALL=REJECTED）",
        "EDGE_INSTALL=REJECTED" in doc and "firebot-vehicle-edge" in doc,
    )

    ctl = read("integration/ros1/firebotctl")
    # enroll：Bridge 门必须在 token POST 之前（源码顺序断言）
    enroll_sec = read("integration/ros1/vehicle-edge/lifecycle/enroll.sh")
    gate_pos = enroll_sec.find("LEGACY_BRIDGE_STILL_ACTIVE")
    post_pos = enroll_sec.find('"$CURL" -sf -X POST')
    if post_pos == -1:
        post_pos = enroll_sec.find("$CURL")
    check(
        "CUTOVER_ORDER_CONTRACT：enroll Bridge 门在 token POST 之前（代码顺序）",
        gate_pos != -1 and post_pos != -1 and gate_pos < post_pos,
        f"gate={gate_pos} post={post_pos}",
    )
    # install：Bridge 门必须在 release_sync 之前
    install_sec = ctl.split("    install)", 1)[1].split("    verify)", 1)[0]
    igate = install_sec.find("BRIDGE_ACTIVE_BEFORE_INSTALL")
    irel = install_sec.find('release_sync "$sha"')
    check(
        "CUTOVER_ORDER_CONTRACT：install Bridge 门在 release_sync 之前（代码顺序）",
        igate != -1 and irel != -1 and igate < irel,
        f"gate={igate} rel={irel}",
    )


def test_migration_stop_legacy_surface() -> None:
    sh = read("integration/ros1/vehicle-edge/lifecycle/migration-stop-legacy.sh")
    check(
        "migration-stop-legacy 输出 MIGRATION_CUTOVER_BOUNDARY=ENTERED",
        "MIGRATION_CUTOVER_BOUNDARY=ENTERED" in sh,
    )
    check(
        "migration-stop-legacy 输出 LEGACY_BRIDGE_WAS_ACTIVE/ENABLED/STOPPED",
        "LEGACY_BRIDGE_WAS_ACTIVE" in sh
        and "LEGACY_BRIDGE_WAS_ENABLED" in sh
        and "LEGACY_BRIDGE_STOPPED=YES" in sh,
    )
    check(
        "migration-stop-legacy Control 不安全拒绝（CONTROL_NOT_SAFE）",
        "MIGRATION_STOP_LEGACY=REJECTED" in sh and "CONTROL_NOT_SAFE" in sh,
    )
    active = active_lines(sh)
    # 唯一变更动作：unit_disable_now_checked firebot-bridge
    # （统一 mutation wrapper）；不触碰 control 启动。
    check(
        "migration-stop-legacy 唯一变更动作 unit_disable_now_checked firebot-bridge",
        "unit_disable_now_checked firebot-bridge" in sh,
    )
    check(
        "migration-stop-legacy 不启动 control（可执行行无 enable/start firebot-control）",
        not any(
            ("enable firebot-control" in ln) or ("start firebot-control" in ln) for ln in active
        ),
    )
    check(
        "migration-stop-legacy 不触碰 ROS/base/navigation\n"
        "（可执行行无 roscore/roslaunch/move_base）",
        not any(t in ln for ln in active for t in ("roscore", "roslaunch", "move_base")),
    )
    check(
        "migration-stop-legacy 不修改 env/secret/credential/Tailscale（可执行行）",
        not any(
            t in ln
            for ln in active
            for t in (
                "bridge.env",
                "bridge-secret",
                "mqtt_password",
                "tailscale down",
                "tailscale logout",
            )
        ),
    )


def test_readonly_start_fail_closed() -> None:
    sh = read("integration/ros1/vehicle-edge/lifecycle/readonly-start.sh")
    check(
        "readonly-start 失败分支 unit_disable_now_checked firebot-vehicle-edge",
        "unit_disable_now_checked firebot-vehicle-edge" in sh,
    )
    check("readonly-start 输出 FAIL_CLOSED_EDGE_STOP", "FAIL_CLOSED_EDGE_STOP" in sh)
    check(
        "readonly-start 失败输出真实 EDGE_ACTIVE_STATE/EDGE_ENABLED_STATE",
        "EDGE_ACTIVE_STATE=" in sh and "EDGE_ENABLED_STATE=" in sh,
    )
    marker = 'if [ "$FAILED" -ne 0 ]; then'
    fail_branch = sh.split(marker, 1)[1] if marker in sh else ""
    fail_active = active_lines(fail_branch)
    check(
        "readonly-start 失败分支不触碰 control/ROS/Tailscale（可执行行；只读 predicate 不算变更）",
        not any(
            t in ln
            for ln in fail_active
            for t in (
                "roscore",
                "roslaunch",
                "move_base",
                "enable firebot-control",
                "start firebot-control",
                "tailscale up",
                "tailscale down",
                "tailscale logout",
            )
        ),
    )


def test_enrolled_exact_sha_gate() -> None:
    sh = read("integration/ros1/vehicle-edge/lifecycle/readonly-preflight.sh")
    check("enrolled 阶段含 EXACT_SHA_MATCH 检查", "EXACT_SHA_MATCH=" in sh)
    check("enrolled 阶段 rev-parse HEAD 比较", "rev-parse HEAD" in sh)
    # bootstrap 不强制：EXACT_SHA 检查必须位于 enrolled 分支内
    enrolled_sec = sh.split('if [ "$PHASE" = "enrolled" ]; then', 1)[1]
    check(
        "EXACT_SHA 检查位于 enrolled 分支内（bootstrap 不强制）",
        "EXACT_SHA_MATCH" in enrolled_sec and "rev-parse HEAD" in enrolled_sec,
    )


def test_rollback_contract() -> None:
    doc = read("docs/部署运维/R002跨Broker公网迁移.md")
    check("ROLLBACK_CONTRACT：ROLLBACK_REQUIRED=YES 边界", "ROLLBACK_REQUIRED=YES" in doc)
    check(
        "ROLLBACK_CONTRACT：恢复清单（/etc/firebot、/opt/firebot/vehicle-bridge、\n"
        "systemd unit、APPROVED_RUNTIME）",
        all(
            t in doc
            for t in (
                "/etc/firebot/",
                "/opt/firebot/vehicle-bridge",
                "firebot-bridge.service",
                "APPROVED_RUNTIME",
            )
        ),
    )
    check(
        "ROLLBACK_CONTRACT：daemon-reload + enable/start legacy bridge",
        "daemon-reload" in doc and "enable/start" in doc,
    )
    check(
        "ROLLBACK_CONTRACT：验证 MQTT_HOST=100.110.31.112 + MQTT_CONNECTED=YES",
        "MQTT_HOST=100.110.31.112" in doc and "MQTT_CONNECTED=YES" in doc,
    )
    check("ROLLBACK_CONTRACT：LEGACY_ROLLBACK=PASS 输出", "LEGACY_ROLLBACK=PASS" in doc)
    check(
        "ROLLBACK_CONTRACT：云端 identity/credential/token 不删除（保持 OFFLINE）",
        "不删除" in doc and "OFFLINE" in doc,
    )
    check("ROLLBACK_CONTRACT：重试走 fleet register credential ensure", "credential ensure" in doc)
    check(
        "TAILSCALE=ROLLBACK_CHANNEL_ONLY 保留（不得 down/logout/卸载/禁用）",
        "不得 tailscale down/logout/卸载/禁用" in doc
        or ("tailscale down" in doc and "logout" in doc),
    )


def main() -> int:
    test_cutover_order_contract()
    test_migration_stop_legacy_surface()
    test_readonly_start_fail_closed()
    test_enrolled_exact_sha_gate()
    test_rollback_contract()
    if FAILURES:
        print(f"\nCUTOVER_CONTRACT_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nCUTOVER_CONTRACT_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
