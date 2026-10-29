#!/usr/bin/env python3
"""目录治理静态合同测试（B0-FIX-CODE / 规格二十九、三十、三十一、三十五、三十八）。

合同：
  SOURCE_ROOT=/home/tl/ROBOT-Web（Git 交付源）
  EDGE_PRODUCTION_ROOT=/home/tl/firebot-edge（生产唯一真源）
  ROS_ROOT=/home/tl/firerobot_ws（ROS 运行时）
  STAGE_EVIDENCE_ROOT=/home/tl/r002-stage-a（仅证据，禁止成为运行时依赖）
  LEGACY_ROOT=/opt/firebot/vehicle-bridge（仅 legacy 回滚兼容）

禁止：
  - 生产 systemd 引用 ROBOT-Web / r002-stage-a / /opt
  - 生产 lifecycle/管理脚本引用 /home/tl/r002-stage-a
  - 新版 edge 安装到 /opt/firebot/vehicle-bridge

运行：cd vehicle-edge && python3 tests/test_directory_governance.py
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
ROS1_SRC = VEHICLE_EDGE_SRC.parent

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def test_systemd_template_governance() -> None:
    tpl = VEHICLE_EDGE_SRC / "systemd" / "firebot-vehicle-edge.service.template"
    text = read(tpl)
    check("systemd template 不引用 ROBOT-Web", "/home/tl/ROBOT-Web" not in text)
    check("systemd template 不引用 r002-stage-a", "/home/tl/r002-stage-a" not in text)
    check("systemd template 不引用 /opt", "/opt" not in text)
    check("systemd template 用 @EDGE_HOME@ 占位", "@EDGE_HOME@" in text)


def test_lifecycle_no_stage_evidence_dependency() -> None:
    offenders = []
    for script in (VEHICLE_EDGE_SRC / "lifecycle").glob("*.sh"):
        if "r002-stage-a" in read(script):
            offenders.append(script.name)
    for script in (VEHICLE_EDGE_SRC / "install").glob("*.sh"):
        if "r002-stage-a" in read(script):
            offenders.append(f"install/{script.name}")
    if "r002-stage-a" in read(ROS1_SRC / "firebotctl"):
        offenders.append("firebotctl")
    check("生产脚本不含 /home/tl/r002-stage-a 运行依赖", not offenders, str(offenders))


def test_edge_install_not_to_opt() -> None:
    ei = VEHICLE_EDGE_SRC / "lifecycle" / "edge-install.sh"
    text = read(ei)
    # edge-install 组装 release 到 EDGE_HOME/releases，绝不写 /opt/firebot/vehicle-bridge
    active = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    writes_opt = [ln for ln in active if "/opt/firebot" in ln and ("cp" in ln or "mv" in ln or "install" in ln)]
    check("edge-install 不安装到 /opt/firebot/vehicle-bridge", not writes_opt, str(writes_opt)[:300])


def test_edge_paths_canonical_roots() -> None:
    lib = VEHICLE_EDGE_SRC / "lib" / "edge_paths.py"
    text = read(lib)
    check("edge_paths 定义默认 Edge Home", 'DEFAULT_EDGE_HOME = "/home/tl/firebot-edge"' in text)
    check("edge_paths 定义 legacy etc（只读 discovery）", 'LEGACY_ETC = "/etc/firebot"' in text)
    check("edge_paths 不引用 r002-stage-a", "r002-stage-a" not in text)
    check("edge_paths 定义 tools release domain", '"tools_releases"' in text and '"tools_current"' in text)


def test_firebotctl_git_guarded_by_layout() -> None:
    # firebotctl 的 Git 依赖必须仅在 SOURCE_TREE 模式；生产模式（VEHICLE_TOOLS_RELEASE）无 git。
    ctl = read(ROS1_SRC / "firebotctl")
    check("firebotctl 识别 VEHICLE_TOOLS_RELEASE 布局", "VEHICLE_TOOLS_RELEASE" in ctl)
    check("firebotctl 生产拒绝 server scope", "require_vehicle_scope" in ctl and "AVAILABLE_SCOPE=vehicle" in ctl)
    check("firebotctl 生产拒绝 legacy rollback", "LEGACY_FULL_INSTALL_COMMAND_NOT_AVAILABLE_IN_EDGE_TOOLS" in ctl)


if __name__ == "__main__":
    test_systemd_template_governance()
    test_lifecycle_no_stage_evidence_dependency()
    test_edge_install_not_to_opt()
    test_edge_paths_canonical_roots()
    test_firebotctl_git_guarded_by_layout()
    print(f"\nDIRECTORY_GOVERNANCE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
