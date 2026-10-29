#!/usr/bin/env python3
"""真实 systemd exit 语义回归测试（B0-FIX3 / REAL_SYSTEMD_EXIT_SEMANTICS）。

真实 systemd：
  is-active  active   → stdout=active   exit 0
  is-active  inactive → stdout=inactive exit 3（非 0）
  is-enabled enabled  → stdout=enabled  exit 0
  is-enabled disabled → stdout=disabled exit 1（非 0）

旧 bug：`$(systemctl is-active X || echo inactive)` 在 inactive(exit!=0) 时拼成
「inactive\\ninactive」，破坏精确比较。本测试用真实 exit 语义的 fake systemctl 证明：
  1. unit_active_state / unit_enabled_state 输出单一状态（无重复行）；unknown fail-closed
  2. migration-stop-legacy cutover boundary 记账（CUTOVER_STARTED / CUTOVER_BOUNDARY_CROSSED / UNCERTAIN）

运行：cd vehicle-edge && python3 tests/test_real_systemd_semantics.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

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


FAKE_SYSTEMCTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    args="$*"
    emit_active() {
      printf '%s\\n' "$1"
      case "$1" in active) exit 0 ;; *) exit 3 ;; esac
    }
    emit_enabled() {
      printf '%s\\n' "$1"
      case "$1" in enabled|linked|linked-runtime|alias) exit 0 ;; *) exit 1 ;; esac
    }
    case "$args" in
      "is-enabled firebot-control") emit_enabled "$(cat "${FAKE_STATE:?}/ctl_enabled" 2>/dev/null || echo disabled)" ;;
      "is-active firebot-control") emit_active "$(cat "$FAKE_STATE/ctl_active" 2>/dev/null || echo inactive)" ;;
      "is-enabled firebot-bridge") emit_enabled "$(cat "$FAKE_STATE/bridge_enabled" 2>/dev/null || echo disabled)" ;;
      "is-active firebot-bridge") emit_active "$(cat "$FAKE_STATE/bridge_active" 2>/dev/null || echo inactive)" ;;
      "is-active firebot-vehicle-edge") emit_active "$(cat "$FAKE_STATE/edge_active" 2>/dev/null || echo inactive)" ;;
      "is-active tailscaled") emit_active "$(cat "$FAKE_STATE/tailscaled_active" 2>/dev/null || echo inactive)" ;;
      "disable --now firebot-bridge") echo "${FAKE_POST_STOP_BRIDGE:-inactive}" > "$FAKE_STATE/bridge_active"; exit 0 ;;
      *) echo "unexpected: $args" >&2; exit 1 ;;
    esac
    """)


def make_state(tmp: Path) -> Path:
    state = tmp / "state"
    state.mkdir()
    (state / "ctl_enabled").write_text("disabled")
    (state / "ctl_active").write_text("inactive")
    (state / "bridge_enabled").write_text("enabled")
    (state / "bridge_active").write_text("active")
    (state / "edge_active").write_text("inactive")
    (state / "tailscaled_active").write_text("active")
    return state


def make_fakebin(tmp: Path) -> Path:
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    fake = fakebin / "systemctl"
    fake.write_text(FAKE_SYSTEMCTL)
    fake.chmod(0o755)
    return fakebin


def env_for(fakebin: Path, state: Path, **extra: str) -> dict:
    e = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
        "FAKE_STATE": str(state),
        "FIREBOT_TEST_SKIP_ROOT": "1",
        "FIREBOT_TEST_SKIP_CHOWN": "1",
    }
    e.update(extra)
    return e


def test_unit_state_helper() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-helper-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        env = env_for(fakebin, state)

        # inactive(exit 3) → 单一 "inactive"（不是 "inactive\\ninactive"）
        (state / "bridge_active").write_text("inactive")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state firebot-bridge'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_active_state inactive → 单一值", r.stdout == "inactive\n", repr(r.stdout))
        check("NO_DOUBLED_INACTIVE", "inactive\ninactive" not in r.stdout)

        # active(exit 0) → "active"
        (state / "bridge_active").write_text("active")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state firebot-bridge'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_active_state active → active", r.stdout == "active\n", repr(r.stdout))

        # disabled(exit 1) → 单一 "disabled"
        (state / "ctl_enabled").write_text("disabled")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_enabled_state firebot-control'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_enabled_state disabled → 单一值", r.stdout == "disabled\n", repr(r.stdout))

        (state / "ctl_enabled").write_text("enabled")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_enabled_state firebot-control'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_enabled_state enabled → enabled", r.stdout == "enabled\n", repr(r.stdout))

        # 未知 unit（fake 不识别 → 无 stdout）→ unknown fail-closed
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state no-such-unit'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_active_state unknown → unknown（fail-closed）", r.stdout == "unknown\n", repr(r.stdout))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_stop_boundary_crossed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-stop-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        env = env_for(fakebin, state, FAKE_POST_STOP_BRIDGE="inactive")
        script = VEHICLE_EDGE_SRC / "lifecycle" / "migration-stop-legacy.sh"
        r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout
        check("migration-stop 成功（legacy active→disable --now→inactive exit3）",
              r.returncode == 0, out[-400:] + r.stderr[-400:])
        check("LEGACY_STOP_ACTION_ISSUED=YES", "LEGACY_STOP_ACTION_ISSUED=YES" in out)
        check("CUTOVER_STARTED=YES", "CUTOVER_STARTED=YES" in out)
        check("CUTOVER_BOUNDARY_CROSSED=YES", "CUTOVER_BOUNDARY_CROSSED=YES" in out)
        check("LEGACY_BRIDGE_ACTIVE=NO", "LEGACY_BRIDGE_ACTIVE=NO" in out)
        check("MIGRATION_CUTOVER_BOUNDARY=ENTERED", "MIGRATION_CUTOVER_BOUNDARY=ENTERED" in out)
        check("无 doubled inactive", "inactive\ninactive" not in out, out[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_stop_boundary_uncertain() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-uncertain-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        env = env_for(fakebin, state, FAKE_POST_STOP_BRIDGE="failed")
        script = VEHICLE_EDGE_SRC / "lifecycle" / "migration-stop-legacy.sh"
        r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout
        check("migration-stop UNCERTAIN fail-closed", r.returncode != 0, out[-400:] + r.stderr[-400:])
        check("MIGRATION_STOP_LEGACY=UNCERTAIN", "MIGRATION_STOP_LEGACY=UNCERTAIN" in out)
        check("CUTOVER_STARTED=YES（stop 已发出）", "CUTOVER_STARTED=YES" in out)
        check("CUTOVER_STATE=UNCERTAIN", "CUTOVER_STATE=UNCERTAIN" in out)
        check("绝不输出 CUTOVER_STARTED=NO", "CUTOVER_STARTED=NO" not in out)
        check("不输出 CUTOVER_BOUNDARY_CROSSED=YES", "CUTOVER_BOUNDARY_CROSSED=YES" not in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_unit_state_helper()
    test_migration_stop_boundary_crossed()
    test_migration_stop_boundary_uncertain()
    print(f"\nREAL_SYSTEMD_SEMANTICS_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
