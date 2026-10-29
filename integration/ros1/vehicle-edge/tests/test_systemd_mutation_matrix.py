#!/usr/bin/env python3
"""systemd 查询/mutation 故障矩阵 + 静态合同测试（B0-FINAL-HARDENING）。

覆盖（spec 34/35/36）：
  - systemd query failure matrix：ABSENT != UNKNOWN、DISABLED != STATIC、DISABLED != ENABLED_RUNTIME
  - mutation 四象限 + unknown 共 5 种（unit_disable_now_checked / unit_enable_now_checked）
  - 静态合同：lifecycle/install 可执行行无 raw systemctl is-active/is-enabled/enable --now/disable --now

运行：cd vehicle-edge && python3 tests/test_systemd_mutation_matrix.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_systemctl import write_fake, set_state

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
LIB = VEHICLE_EDGE_SRC / "lib"

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


def run_helper(tmp: Path, state: Path, script: str, **env_extra: str) -> subprocess.CompletedProcess:
    fakebin = tmp / "fakebin"
    write_fake(fakebin)
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
        "FAKE_STATE": str(state),
    }
    env.update(env_extra)
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


def test_query_failure_matrix() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sysd-query-"))
    try:
        state = tmp / "state"
        state.mkdir()
        # ABSENT（LoadState=not-found）!= UNKNOWN
        set_state(state, "firebot-control", load="not-found")
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; unit_load_state firebot-control')
        check("ABSENT → LoadState=not-found（非 unknown）", r.stdout == "not-found\n", repr(r.stdout))
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; control_is_safe')
        check("ABSENT → CONTROL_SAFE=YES + UNIT_PRESENT=NO",
              "CONTROL_SAFE=YES" in r.stdout and "CONTROL_UNIT_PRESENT=NO" in r.stdout, r.stdout)
        # UNKNOWN（show 失败）→ CONTROL_SAFE=NO
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; control_is_safe', FAKE_SHOW_FAIL="1")
        check("UNKNOWN → CONTROL_SAFE=NO", "CONTROL_SAFE=NO" in r.stdout, r.stdout)
        # DISABLED != STATIC（static 不自动安全）
        set_state(state, "firebot-control", load="loaded", active="inactive", enabled="static")
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; control_is_safe')
        check("STATIC 不视为 safe（FAIL_CLOSED）", "CONTROL_SAFE=NO" in r.stdout, r.stdout)
        # DISABLED != ENABLED_RUNTIME
        set_state(state, "firebot-control", load="loaded", active="inactive", enabled="enabled-runtime")
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; control_is_safe')
        check("ENABLED_RUNTIME 不视为 safe", "CONTROL_SAFE=NO" in r.stdout, r.stdout)
        # DISABLED → safe
        set_state(state, "firebot-control", load="loaded", active="inactive", enabled="disabled")
        r = run_helper(tmp, state, f'. "{LIB}/systemd_state.sh"; control_is_safe')
        check("DISABLED → CONTROL_SAFE=YES", "CONTROL_SAFE=YES" in r.stdout, r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mutation_matrix() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sysd-mut-"))
    try:
        state = tmp / "state"
        state.mkdir()
        set_state(state, "firebot-bridge", load="loaded", active="active", enabled="enabled")
        # CASE1 RC=0 post 正确
        set_state(state, "firebot-bridge", load="loaded", active="active", enabled="enabled")
        r = run_helper(tmp, state,
                       f'. "{LIB}/systemd_state.sh"; unit_disable_now_checked firebot-bridge; '
                       f'echo RC=$SYSTEMD_MUTATION_RC A=$POST_ACTIVE_STATE E=$POST_ENABLED_STATE')
        check("MUTATION_RC0_POST_OK", "RC=0" in r.stdout and "A=inactive" in r.stdout and "E=disabled" in r.stdout, r.stdout)
        # CASE2 RC=0 post 错误
        r = run_helper(tmp, state,
                       f'. "{LIB}/systemd_state.sh"; unit_disable_now_checked firebot-bridge; '
                       f'echo RC=$SYSTEMD_MUTATION_RC A=$POST_ACTIVE_STATE',
                       FAKE_POST_ACTIVE="active")
        check("MUTATION_RC0_POST_BAD", "RC=0" in r.stdout and "A=active" in r.stdout, r.stdout)
        # CASE3 RC!=0 post 正确（副作用已发生）
        r = run_helper(tmp, state,
                       f'. "{LIB}/systemd_state.sh"; unit_disable_now_checked firebot-bridge; '
                       f'echo RC=$SYSTEMD_MUTATION_RC A=$POST_ACTIVE_STATE', FAKE_MUTATION_RC="1")
        check("MUTATION_RCNONZERO_POST_OK", "RC=1" in r.stdout and "A=inactive" in r.stdout, r.stdout)
        # CASE4 RC!=0 post 错误
        r = run_helper(tmp, state,
                       f'. "{LIB}/systemd_state.sh"; unit_disable_now_checked firebot-bridge; '
                       f'echo RC=$SYSTEMD_MUTATION_RC A=$POST_ACTIVE_STATE',
                       FAKE_MUTATION_RC="1", FAKE_POST_ACTIVE="active")
        check("MUTATION_RCNONZERO_POST_BAD", "RC=1" in r.stdout and "A=active" in r.stdout, r.stdout)
        # CASE5 RC!=0 post unknown
        r = run_helper(tmp, state,
                       f'. "{LIB}/systemd_state.sh"; unit_disable_now_checked firebot-bridge; '
                       f'echo RC=$SYSTEMD_MUTATION_RC A=$POST_ACTIVE_STATE',
                       FAKE_MUTATION_RC="1", FAKE_SHOW_FAIL="1")
        check("MUTATION_RCNONZERO_POST_UNKNOWN", "RC=1" in r.stdout and "A=unknown" in r.stdout, r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_static_no_raw_systemctl() -> None:
    """spec 34：lifecycle/install 可执行行禁止 raw systemctl 状态/mutation gate。"""
    offenders = []
    for f in list((VEHICLE_EDGE_SRC / "lifecycle").glob("*.sh")) + list((VEHICLE_EDGE_SRC / "install").glob("*.sh")):
        for ln in f.read_text().splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if ("is-active" in s and "unit_active_state" not in s) \
               or ("is-enabled" in s and "unit_enabled_state" not in s) \
               or ("enable --now" in s) or ("disable --now" in s):
                offenders.append(f"{f.name}:{s}")
    check("RAW_IS_ACTIVE_GATE_COUNT=0 + RAW_IS_ENABLED_GATE_COUNT=0（无 raw systemctl gate）",
          not offenders, "; ".join(offenders[:3]))
    # mutation wrapper 只在 lib/systemd_state.sh
    lib = (LIB / "systemd_state.sh").read_text()
    check("mutation 统一在 lib/systemd_state.sh（unit_disable_now_checked/unit_enable_now_checked）",
          "unit_disable_now_checked" in lib and "unit_enable_now_checked" in lib)


if __name__ == "__main__":
    test_query_failure_matrix()
    test_mutation_matrix()
    test_static_no_raw_systemctl()
    print(f"\nSYSTEMD_MUTATION_MATRIX_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
