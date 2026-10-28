#!/usr/bin/env python3
"""Vehicle Edge 工程化合同测试（VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION，静态断言）。

锁定：
  - firebotctl = Fleet + Lifecycle dispatcher（enroll/readonly/migration 转发
    vehicle-edge；edge-init/edge-install/edge-verify 接线；vehicle install 标 LEGACY）
  - 服务器 Fleet 脚本留在 integration/ros1/（不进 vehicle-edge）
  - 旧三脚本是 thin shim（NO_DUPLICATED_PRODUCTION_LOGIC）
  - NO_VEHICLE_SPECIFIC_PRODUCTION_CODE（vehicle-edge 无 R00x 目录 /
    if vehicle_id=="R002" 生产代码）
  - Control 不进 vehicle-edge（edge 脚本不触碰 firerobot_ws/catkin/
    firebot-control 启动/roscore/cmd_vel）
  - systemd Conflicts=firebot-bridge.service（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）
  - 权限模型（edge-init 700/750/644/0600 语义）
  - runtime user 显式配置（禁止 id -un 自动猜 → fail-closed）
  - 状态模型 exit code（0/10/20/30/40/50）

用法：python3 scripts/test_edge_contract.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROS1 = ROOT / "integration" / "ros1"
EDGE = ROS1 / "vehicle-edge"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def active_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_firebotctl_dispatcher() -> None:
    ctl = read(ROS1 / "firebotctl")
    check("firebotctl edge-init 接线", "edge-init)" in ctl and "lifecycle/edge-init.sh" in ctl)
    check(
        "firebotctl edge-install 接线",
        "edge-install)" in ctl and "lifecycle/edge-install.sh" in ctl,
    )
    check(
        "firebotctl edge-verify 接线",
        "edge-verify)" in ctl and "lifecycle/edge-verify.sh" in ctl,
    )
    check("firebotctl enroll 转发 vehicle-edge", "lifecycle/enroll.sh" in ctl)
    check(
        "firebotctl readonly-preflight 转发 vehicle-edge", "lifecycle/readonly-preflight.sh" in ctl
    )
    check("firebotctl readonly-start 转发 vehicle-edge", "lifecycle/readonly-start.sh" in ctl)
    check(
        "firebotctl migration-stop-legacy 转发 vehicle-edge",
        "lifecycle/migration-stop-legacy.sh" in ctl,
    )
    check(
        "vehicle install 标 LEGACY / NOT FOR READONLY CONNECTIVITY MIGRATION",
        "LEGACY / FULL VEHICLE INSTALL" in ctl and "NOT FOR READONLY CONNECTIVITY MIGRATION" in ctl,
    )
    # edge-install exact SHA 门
    check("edge-install HEAD==SHA 门（HEAD_SHA_MISMATCH）", "HEAD_SHA_MISMATCH" in ctl)


def test_fleet_scripts_stay_at_ros1_root() -> None:
    for name in ("fleet-sites.sh", "fleet-preregister.sh", "fleet-register.sh"):
        check(
            f"服务器 Fleet 脚本留在 integration/ros1/{name}",
            (ROS1 / name).exists() and not (EDGE / "lifecycle" / name).exists(),
        )


def test_legacy_shims_thin() -> None:
    for name in ("readonly-preflight.sh", "readonly-start.sh", "migration-stop-legacy.sh"):
        sh = read(ROS1 / name)
        lines = active_lines(sh)
        check(
            f"{name} 是 thin shim（可执行行只有 SELF_DIR/exec）",
            len(lines) <= 2 and "vehicle-edge/lifecycle" in sh and "exec" in sh,
            "; ".join(lines),
        )


def test_no_vehicle_specific_production_code() -> None:
    # vehicle-edge 下禁止 R00x 目录
    dirs = [p.name for p in EDGE.rglob("*") if p.is_dir() and p.name not in ("__pycache__",)]
    check(
        "NO_VEHICLE_SPECIFIC_PRODUCTION_CODE（无 R00x 目录）",
        not any(d == "R002" or d.startswith("R00") for d in dirs),
    )
    # vehicle-edge 生产代码禁止 if vehicle_id == "R002"
    hits = []
    for f in EDGE.rglob("*.py"):
        if "tests" in f.parts:
            continue
        for i, ln in enumerate(read(f).splitlines(), 1):
            if "R002" in ln and ("vehicle_id" in ln or "R002-edge" in ln):
                hits.append(f"{f.name}:{i}")
    for f in EDGE.glob("lifecycle/*.sh"):
        for i, ln in enumerate(read(f).splitlines(), 1):
            if '== "R002"' in ln or "R002-edge" in ln:
                hits.append(f"{f.name}:{i}")
    check(
        "NO_VEHICLE_SPECIFIC_PRODUCTION_CODE（无 vehicle_id==R002 生产代码）",
        not hits,
        "; ".join(hits),
    )


def test_control_stays_out_of_edge() -> None:
    for sh in EDGE.glob("lifecycle/*.sh"):
        text = read(sh)
        active = active_lines(text)
        # 只读检测语义（"$PGREP" -f "rosmaster|roscore" 等 fail-closed 检查）合法；只禁触碰。
        offenders = [
            ln
            for ln in active
            if "$PGREP" not in ln
            and any(
                t in ln
                for t in (
                    "firerobot_ws",
                    "catkin_make",
                    "roscore",
                    "roslaunch",
                    "move_base",
                    "cmd_vel",
                    "enable firebot-control",
                    "start firebot-control",
                )
            )
        ]
        check(
            f"Control 不进 vehicle-edge（{sh.name} 可执行行不触碰 ROS/Control）",
            not offenders,
            "; ".join(offenders[:2]),
        )


def test_systemd_mutual_exclusion() -> None:
    unit = read(EDGE / "systemd" / "firebot-vehicle-edge.service.template")
    check(
        "systemd Conflicts=firebot-bridge.service（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）",
        "Conflicts=firebot-bridge.service" in unit,
    )
    check("systemd ExecStart=firebot-edge run", "@EDGE_HOME@/bin/firebot-edge run" in unit)
    check(
        "systemd 注入 FIREBOT_BRIDGE_STATUS_FILE（复用，不重造 status writer）",
        "FIREBOT_BRIDGE_STATUS_FILE=" in unit,
    )
    check("systemd User=@RUNTIME_USER@", "User=@RUNTIME_USER@" in unit)


def test_permission_model() -> None:
    sh = read(EDGE / "lifecycle" / "edge-init.sh")
    check("权限模型：secrets/backups 0700", "chmod 0700" in sh)
    check("权限模型：runtime/data/logs 0750", "chmod 0750" in sh)
    check("权限模型：config 0644（install.env）", "chmod 0644" in sh)
    check("权限模型：EDGE_HOME 0755", "chmod 0755" in sh)


def test_runtime_user_explicit() -> None:
    sh = read(EDGE / "lifecycle" / "edge-init.sh")
    check("runtime user 显式（禁止 id -un 自动猜）", "--runtime-user" in sh and "禁止 id -un" in sh)
    inst = read(EDGE / "lifecycle" / "edge-install.sh")
    check(
        "EDGE_RUNTIME_USER_NOT_CONFIGURED fail-closed", "EDGE_RUNTIME_USER_NOT_CONFIGURED" in inst
    )
    check("edge-install 绝不 id -un 自动猜", "id -un" not in inst)


def test_health_exit_codes() -> None:
    h = read(EDGE / "monitor" / "health.py")
    check(
        "状态模型 exit code 0/10/20/30/40/50",
        '"HEALTHY": 0' in h
        and '"DEGRADED": 10' in h
        and '"OFFLINE": 20' in h
        and '"UNSAFE": 30' in h
        and '"CONFIG_ERROR": 40' in h
        and '"INTERNAL_ERROR": 50' in h,
    )


def test_enroll_contract_unchanged() -> None:
    sh = read(EDGE / "lifecycle" / "enroll.sh")
    check("enrollment API 路径合同不变（/api/v1/enrollment/activate）", "enrollment/activate" in sh)
    check("enroll 系统 CA（无 -k/--cacert）", "系统 CA" in sh or "禁止 -k" in sh)
    check(
        "enroll 输出到 Edge Home（config/identity、secrets/mqtt.env）",
        "identity.env" in sh and "secrets/mqtt.env" in sh,
    )
    check("enroll cutover 门（LEGACY_BRIDGE_STILL_ACTIVE）", "LEGACY_BRIDGE_STILL_ACTIVE" in sh)


def main() -> int:
    test_firebotctl_dispatcher()
    test_fleet_scripts_stay_at_ros1_root()
    test_legacy_shims_thin()
    test_no_vehicle_specific_production_code()
    test_control_stays_out_of_edge()
    test_systemd_mutual_exclusion()
    test_permission_model()
    test_runtime_user_explicit()
    test_health_exit_codes()
    test_enroll_contract_unchanged()
    if FAILURES:
        print(f"\nEDGE_CONTRACT_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nEDGE_CONTRACT_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
