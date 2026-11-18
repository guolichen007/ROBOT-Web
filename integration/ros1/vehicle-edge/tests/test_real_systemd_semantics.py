#!/usr/bin/env python3
"""真实 systemd exit 语义回归（G4-Fleet-R2 移植 90d781f 的 lib/systemd_state.sh）。

真实 systemctl：is-active inactive → stdout=inactive 且 exit 3；is-enabled disabled → exit 1。
旧写法 `$(systemctl is-active X 2>/dev/null || echo inactive)` 会得到 "inactive\\ninactive"，
使 `[ "$state" = "inactive" ]` 恒假 —— 真车上 enroll / edge-install 永远 REJECTED。
现有测试 fixture 的 fake systemctl 一律 exit 0，掩盖了该缺陷。

本测试：
  HELPER_SINGLE_VALUE   unit_active_state/unit_enabled_state 在真实语义下返回单值；未知 → unknown
  ENROLL_REAL_SEMANTICS 全部 inactive 时 enroll 不得以 LEGACY_BRIDGE_STILL_ACTIVE 拒绝
  NO_ANTIPATTERN        lifecycle/*.sh、firebotctl 可执行行无 `|| echo inactive|disabled`
用法：python3 tests/test_real_systemd_semantics.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1]
ROS1 = EDGE.parent
FAILURES: list = []

REAL_SYSTEMCTL = """#!/bin/bash
# 真实 systemd exit 语义替身
S="$FAKE_STATE"; cmd="$1"; u="$2"
case "$cmd" in
  is-active) st="$(cat "$S/$u.active" 2>/dev/null || echo inactive)"; echo "$st"
             [ "$st" = active ] && exit 0 || exit 3 ;;
  is-enabled) st="$(cat "$S/$u.enabled" 2>/dev/null || echo disabled)"; echo "$st"
              [ "$st" = enabled ] && exit 0 || exit 1 ;;
  *) exit 0 ;;
esac
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)[:300]))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        sc = tmp / "systemctl"
        sc.write_text(REAL_SYSTEMCTL)
        sc.chmod(0o755)
        state = tmp / "state"
        state.mkdir()
        env = dict(os.environ, FIREBOT_TEST_SYSTEMCTL=str(sc), SYSTEMCTL=str(sc), FAKE_STATE=str(state))
        helper = EDGE / "lib" / "systemd_state.sh"
        r = subprocess.run(
            ["bash", "-c", '. "$1"; printf "[%s][%s]" "$(unit_active_state x)" "$(unit_enabled_state x)"', "_",
             str(helper)], env=env, capture_output=True, text=True)
        check("HELPER_SINGLE_VALUE inactive/disabled", r.stdout == "[inactive][disabled]", r.stdout)
        (state / "x.active").write_text("weird\n")
        r = subprocess.run(["bash", "-c", '. "$1"; unit_active_state x', "_", str(helper)],
                           env=env, capture_output=True, text=True)
        check("HELPER_UNKNOWN fail-closed", r.stdout.strip() == "unknown", r.stdout)
        # 审查 SHOULD-FIX 5：unknown（bad / D-Bus 不可达）与 enabled-runtime 必须 fail-closed
        (state / "c.enabled").write_text("enabled-runtime\n")
        (state / "d.enabled").write_text("bad\n")
        r = subprocess.run(
            ["bash", "-c", '. "$1"; printf "[%s][%s]" "$(unit_enabled_state c)" "$(unit_enabled_state d)";'
             ' unit_is_surely_disabled c && echo C-DIS; unit_is_surely_disabled d && echo D-DIS;'
             ' unit_is_surely_stopped x && echo X-STOP; true', "_", str(helper)],
            env=env, capture_output=True, text=True)
        check("FAIL_CLOSED enabled-runtime→enabled、bad→unknown，二者均非“确定未启用”；unknown 非“确定已停”",
              r.stdout.startswith("[enabled][unknown]") and "C-DIS" not in r.stdout
              and "D-DIS" not in r.stdout and "X-STOP" not in r.stdout, r.stdout)
        r = subprocess.run(["bash", "-c", 'x="$("$SYSTEMCTL" is-active y 2>/dev/null || echo inactive)"; printf "%s" "$x"'],
                           env=env, capture_output=True, text=True)
        check("ANTIPATTERN_REPRO 旧写法在真实语义下得到两行", r.stdout == "inactive\ninactive", repr(r.stdout))

        home = tmp / "edge"
        (home / "certs").mkdir(parents=True)
        (home / "certs/mqtt-ca.crt").write_text("CA\n")
        (home / "config").mkdir()
        (home / "config/bootstrap.env").write_text("FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n")
        resp = {"device_id": "firebot-vehicle-03", "transport_profile": "u-dao-standard-v1",
                "protocol": "canonical-mqtt-1.3", "mqtt_host": "firebot.youdaozd.com", "mqtt_port": 8883,
                "mqtt_password": "x", "device_token": "y"}
        (tmp / "resp.json").write_text(json.dumps(resp))
        curl = tmp / "curl"
        curl.write_text("#!/bin/bash\ncat >/dev/null\ncat %s\n" % (tmp / "resp.json"))
        curl.chmod(0o755)
        tok = tmp / "tok"
        tok.write_text("ab" * 32)
        tok.chmod(0o600)
        env.update(FIREBOT_EDGE_HOME=str(home), FIREBOT_TEST_CURL=str(curl), FIREBOT_TEST_SKIP_ROOT="1")
        r = subprocess.run(["bash", str(EDGE / "lifecycle/enroll.sh"), "firebot-vehicle-03", "--token-file", str(tok)],
                           env=env, capture_output=True, text=True, timeout=60)
        check("ENROLL_REAL_SEMANTICS 全部 inactive → 不误拒（ENROLL=PASS）",
              r.returncode == 0 and "ENROLL=PASS" in r.stdout, r.stdout + r.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        sc = tmp / "systemctl"
        sc.write_text(REAL_SYSTEMCTL)
        sc.chmod(0o755)
        state = tmp / "state"
        state.mkdir()
        (state / "firebot-control.enabled").write_text("bad\n")
        home = tmp / "edge"
        (home / "config").mkdir(parents=True)
        (home / "certs").mkdir()
        env = dict(os.environ, FIREBOT_TEST_SYSTEMCTL=str(sc), FAKE_STATE=str(state),
                   FIREBOT_EDGE_HOME=str(home), FIREBOT_TEST_SKIP_ROOT="1")
        r = subprocess.run(["bash", str(EDGE / "lifecycle/readonly-start.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        check("FAIL_CLOSED readonly-start：Control 状态 unknown → REJECTED（不启动）",
              r.returncode != 0 and "CONTROL_ENABLED=yes_or_unknown" in r.stdout
              and "enable --now" not in (state / "calls").read_text() if (state / "calls").exists()
              else r.returncode != 0 and "CONTROL_ENABLED=yes_or_unknown" in r.stdout, r.stdout[-300:])

    bad = []
    for f in list((EDGE / "lifecycle").glob("*.sh")) + [ROS1 / "firebotctl"]:
        for n, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            if re.search(r"is-(active|enabled)[^)]*\|\|\s*echo\s+(inactive|disabled)", ln):
                bad.append("%s:%d" % (f.name, n))
    check("NO_ANTIPATTERN（lifecycle + firebotctl）", not bad, bad)
    if FAILURES:
        print("\nREAL_SYSTEMD_SEMANTICS_TEST=FAIL count=%d" % len(FAILURES))
        return 1
    print("\nREAL_SYSTEMD_SEMANTICS_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
