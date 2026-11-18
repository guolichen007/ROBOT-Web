#!/usr/bin/env python3
"""firebotctl vehicle media 生命周期行为测试（fixture：fake systemctl / 临时 Edge Home / fake repo）。

覆盖（全部经 firebotctl 正式入口）：
  INSTALL_NOT_START        install 后 firebot-media@* inactive+disabled；unit 渲染正确
  PROTECTED_UNITS          install/credentials/start/stop 不改变 G1 readonly agent / edge / control 状态
                           （001 增量：readonly agent active 期间安装与启动媒体）
  INSTALL_INTERRUPTED      libsrt 构建失败 → staging 清理、current 不变、无半成品 release
  RELEASE_EXISTS_VERIFIED  同 SHA 重试安全；篡改 release → 拒绝
  CREDENTIAL_IMPORT        bundle 文件 0600 / stdin；错误身份、主密钥字段、argv 传参一律拒绝；
                           写入 media/secrets 0600、目录 0700；值不出现在输出
  ROLLBACK                 两次安装 → rollback 切回 previous 且服务保持停止
  START_REQUIRES_PREFLIGHT 无凭证 → start 拒绝（不 enable）
运行：python3 tests/test_media_lifecycle.py
环境：FIREBOT_MEDIA_LIBSRT_PREBUILT=<dir 含 libsrt.so.1.4.4(+.sha256)> 时跑完整安装路径；
      缺省时以 stub 共享库替代（只验证生命周期，不验证 SRT）。
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

ROS1 = Path(__file__).resolve().parents[3]  # integration/ros1
REPO = ROS1.parents[1]
FAILURES: list = []
TOKEN = "b" * 64
PASSPHRASE = "media-lifecycle-test-pass"
VID = "firebot-vehicle-01"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)[:400]))
    if not cond:
        FAILURES.append(name)


FAKE_SYSTEMCTL = r"""#!/bin/bash
# fake systemctl：状态存于 $FAKE_SD_STATE/<unit>.{active,enabled}；所有调用记录到 calls.log
S="$FAKE_SD_STATE"; echo "$*" >> "$S/calls.log"
cmd="$1"; shift
[ "$cmd" = "disable" ] && [ "$1" = "--now" ] && { shift; for u in "$@"; do echo inactive > "$S/$u.active"; echo disabled > "$S/$u.enabled"; done; exit 0; }
[ "$cmd" = "enable" ] && [ "$1" = "--now" ] && { shift; for u in "$@"; do echo active > "$S/$u.active"; echo enabled > "$S/$u.enabled"; done; exit 0; }
case "$cmd" in
  is-active) cat "$S/$1.active" 2>/dev/null || echo inactive; [ "$(cat "$S/$1.active" 2>/dev/null)" = active ] ;;
  is-enabled) cat "$S/$1.enabled" 2>/dev/null || echo disabled; [ "$(cat "$S/$1.enabled" 2>/dev/null)" = enabled ] ;;
  enable) for u in "$@"; do echo enabled > "$S/$u.enabled"; done ;;
  disable) for u in "$@"; do echo disabled > "$S/$u.enabled"; done ;;
  start|restart) for u in "$@"; do echo active > "$S/$u.active"; done ;;
  stop) for u in "$@"; do echo inactive > "$S/$u.active"; done ;;
  daemon-reload) : ;;
esac
exit 0
"""


class Fx:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="media-lc-"))
        self.repo = self.tmp / "repo"
        shutil.copytree(
            ROS1,
            self.repo / "integration" / "ros1",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "firebot-001-software"),
        )
        for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "fx"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *cmd],
                cwd=self.repo,
                check=True,
                capture_output=True,
            )
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.edge = self.tmp / "edge"
        (self.edge / "config").mkdir(parents=True)
        (self.edge / "config" / "install.env").write_text(
            "FIREBOT_EDGE_USER=tl\nFIREBOT_EDGE_HOME=%s\n" % self.edge
        )
        self.sd = self.tmp / "sd"
        self.sd.mkdir()
        self.units = self.tmp / "units"
        self.units.mkdir()
        fake = self.tmp / "systemctl"
        fake.write_text(FAKE_SYSTEMCTL)
        fake.chmod(0o755)
        self.fake = fake
        # 001 增量：G1 readonly agent 正在运行
        self.set_unit("firebot-vehicle-agent-v1-readonly", "active", "enabled")
        self.libsrt = self._libsrt()

    def _libsrt(self) -> Path:
        pre = os.environ.get("FIREBOT_MEDIA_LIBSRT_PREBUILT")
        if pre and Path(pre, "libsrt.so.1.4.4").is_file():
            return Path(pre)
        d = self.tmp / "libsrt-stub"
        d.mkdir()
        src = d / "stub.c"
        src.write_text(
            "unsigned int srt_getversion(void){return 0x010404;}\n"
            "int srt_startup(void){return 0;}\nvoid srt_setloglevel(int l){(void)l;}\n"
            'const char* srt_getlasterror_str(void){return "stub";}\nint srt_create_socket(void){return -1;}\n'
            "int srt_sendmsg(int a,const char*b,int c,int d,int e){return -1;}\n"
        )
        r = subprocess.run(
            ["cc", "-shared", "-fPIC", "-o", str(d / "libsrt.so.1.4.4"), str(src)],
            capture_output=True,
        )
        if r.returncode != 0:
            raise SystemExit("cc 不可用，无法构建 stub libsrt: %s" % r.stderr)
        h = subprocess.run(
            ["sha256sum", str(d / "libsrt.so.1.4.4")], capture_output=True, text=True
        ).stdout.split()[0]
        (d / "libsrt.so.1.4.4.sha256").write_text(h)
        return d

    def set_unit(self, unit, active, enabled):
        (self.sd / ("%s.active" % unit)).write_text(active + "\n")
        (self.sd / ("%s.enabled" % unit)).write_text(enabled + "\n")

    def unit(self, unit):
        def r(k):
            p = self.sd / ("%s.%s" % (unit, k))
            return (
                p.read_text().strip()
                if p.exists()
                else ("inactive" if k == "active" else "disabled")
            )

        return r("active"), r("enabled")

    def env(self, **extra):
        e = dict(os.environ)
        e.update(
            FIREBOT_EDGE_HOME=str(self.edge),
            FIREBOT_TEST_SYSTEMCTL=str(self.fake),
            FIREBOT_TEST_SYSTEMD_DIR=str(self.units),
            FIREBOT_TEST_SKIP_ROOT="1",
            FIREBOT_TEST_SKIP_CHOWN="1",
            FAKE_SD_STATE=str(self.sd),
            FIREBOT_MEDIA_PYTHON=os.environ.get("FIREBOT_MEDIA_TEST_PYTHON", sys.executable),
        )
        e.update(extra)
        return e

    def ctl(self, *args, stdin=None, **extra):
        return subprocess.run(
            ["bash", str(self.repo / "integration/ros1/firebotctl"), "vehicle", "media", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(**extra),
            timeout=120,
        )

    def commit_change(self):
        f = self.repo / "integration/ros1/vehicle-edge/media/firebot_media/__init__.py"
        f.write_text(f.read_text() + "\n# rev2\n")
        for cmd in (["add", "."], ["commit", "-qm", "rev2"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *cmd],
                cwd=self.repo,
                check=True,
                capture_output=True,
            )
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()


def bundle(fx: Fx, vid=VID, extra=None) -> Path:
    data = {
        "kind": "firebot.media-credentials.v1",
        "vehicle_id": vid,
        "srt_publish_token": TOKEN,
        "srt_publish_passphrase": PASSPHRASE,
    }
    data.update(extra or {})
    p = fx.tmp / ("bundle-%s.json" % len(list(fx.tmp.glob("bundle-*"))))
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def mode(p: Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))[2:]


def main() -> int:
    fx = Fx()
    try:
        g1_before = fx.unit("firebot-vehicle-agent-v1-readonly")
        # -------- install interrupted: bad libsrt prebuilt sha
        bad = fx.tmp / "bad-libsrt"
        shutil.copytree(fx.libsrt, bad)
        (bad / "libsrt.so.1.4.4.sha256").write_text("0" * 64)
        r = fx.ctl("install", "--sha", fx.sha, "--libsrt-prebuilt", str(bad))
        rel = fx.edge / "media" / "releases"
        check("INSTALL_INTERRUPTED 失败返回非 0", r.returncode != 0, r.stdout + r.stderr)
        check(
            "INSTALL_INTERRUPTED 无半成品 release / current 未创建",
            not (rel / fx.sha).exists()
            and not (fx.edge / "media/current").exists()
            and not list(rel.glob(".staging*")),
            list(rel.iterdir()) if rel.exists() else None,
        )
        # -------- install ok
        r = fx.ctl("install", "--sha", fx.sha, "--libsrt-prebuilt", str(fx.libsrt))
        check(
            "INSTALL PASS",
            r.returncode == 0 and "MEDIA_INSTALL=PASS" in r.stdout,
            r.stdout + r.stderr,
        )
        check(
            "INSTALL_NOT_START",
            "MEDIA_SERVICE_STATE=inactive" in r.stdout
            and fx.unit("firebot-media@roof_rgb") == ("inactive", "disabled"),
        )
        unit = (fx.units / "firebot-media@.service").read_text()
        check(
            "UNIT 渲染（EDGE_HOME/用户替换、无占位符）",
            "@EDGE_HOME@" not in unit and str(fx.edge) in unit and "User=firebot-media" in unit,
        )
        check(
            "PROTECTED_UNITS install 后 G1 readonly agent 不变",
            fx.unit("firebot-vehicle-agent-v1-readonly") == g1_before
            and "MEDIA_PROTECTED_UNITS_UNCHANGED=PASS" in r.stdout,
        )
        calls = (fx.sd / "calls.log").read_text()
        check(
            "PROTECTED_UNITS 未对受保护单元调用 stop/restart/disable",
            not any(
                w in calls
                for w in (
                    "stop firebot-vehicle-agent",
                    "restart firebot-vehicle-agent",
                    "disable firebot-vehicle-agent",
                    "firebot-vehicle-edge.service",
                )
            ),
        )
        check(
            "MEDIA layout 权限（secrets 0700 / state 0750）",
            mode(fx.edge / "media/secrets") == "700" and mode(fx.edge / "media/state") == "750",
        )
        check("Hardware Profile 落位", (fx.edge / "config/media-hardware.env").is_file())
        r2 = fx.ctl("install", "--sha", fx.sha, "--libsrt-prebuilt", str(fx.libsrt))
        check(
            "RELEASE_EXISTS_VERIFIED 同 SHA 重试",
            r2.returncode == 0 and "EXISTS_VERIFIED" in r2.stdout,
            r2.stdout + r2.stderr,
        )
        # -------- start without credentials → refused
        r = fx.ctl("start")
        check(
            "START_REQUIRES_PREFLIGHT 无凭证 start 拒绝",
            r.returncode != 0 and fx.unit("firebot-media@roof_rgb")[1] == "disabled",
            r.stdout + r.stderr,
        )
        # -------- credentials: negatives
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--token",
            TOKEN,
        )
        check("CREDENTIAL argv 传凭证被拒", r.returncode != 0 and "禁止经命令行" in r.stderr)
        b = bundle(fx, vid="firebot-vehicle-02")
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--bundle-file",
            str(b),
        )
        check(
            "CREDENTIAL 错误身份 bundle 被拒",
            r.returncode != 0 and not (fx.edge / "media/secrets/srt_publish_token").exists(),
        )
        b = bundle(fx, extra={"media_publish_token": "MASTER-SHOULD-NEVER-LEAVE-SERVER"})
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--bundle-file",
            str(b),
        )
        check(
            "CREDENTIAL 含主密钥字段被拒",
            r.returncode != 0 and "MASTER-SHOULD" not in r.stdout + r.stderr,
        )
        b = bundle(fx)
        b.chmod(0o644)
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--bundle-file",
            str(b),
        )
        check("CREDENTIAL bundle 文件 0644 被拒", r.returncode != 0)
        (fx.edge / "config/identity.env").write_text("DEVICE_ID=firebot-vehicle-03\n")
        b.chmod(0o600)
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--bundle-file",
            str(b),
        )
        check("CREDENTIAL 与 Enrollment 身份不一致被拒", r.returncode != 0 and "不一致" in r.stderr)
        (fx.edge / "config/identity.env").write_text("DEVICE_ID=%s\n" % VID)
        # -------- credentials: stdin ok
        r = fx.ctl(
            "credentials",
            "import",
            "--vehicle-id",
            VID,
            "--srt-host",
            "media.example.test",
            "--srt-port",
            "8890",
            "--bundle-stdin",
            stdin=b.read_text(),
        )
        tok = fx.edge / "media/secrets/srt_publish_token"
        check(
            "CREDENTIAL stdin 导入 PASS",
            r.returncode == 0 and "IMPORTED" in r.stdout,
            r.stdout + r.stderr,
        )
        check(
            "CREDENTIAL 文件 0600",
            mode(tok) == "600" and mode(fx.edge / "media/secrets/srt_publish_passphrase") == "600",
        )
        check(
            "CREDENTIAL 输出不含凭证值",
            TOKEN not in r.stdout + r.stderr and PASSPHRASE not in r.stdout + r.stderr,
        )
        envtxt = (fx.edge / "config/media.env").read_text()
        check(
            "media.env 非 secret（无 token/passphrase）",
            TOKEN not in envtxt
            and PASSPHRASE not in envtxt
            and "FIREBOT_MEDIA_VEHICLE_ID=%s" % VID in envtxt,
        )
        check(
            "CREDENTIAL 不触碰 MQTT（无 secrets/mqtt.env 写入）",
            not (fx.edge / "secrets/mqtt.env").exists(),
        )
        # -------- preflight (stub/real libsrt; encoder auto→libx264 probe needs ffmpeg)
        r = fx.ctl("preflight")
        check(
            "PREFLIGHT PASS（配置/凭证/libsrt/编码器）",
            r.returncode == 0 and "MEDIA_PREFLIGHT=PASS" in r.stdout,
            r.stdout + r.stderr,
        )
        check("PREFLIGHT 输出无 secret", TOKEN not in r.stdout + r.stderr)
        # -------- start/stop single camera; G1 untouched
        r = fx.ctl("start", "roof_thermal")
        check(
            "START 单路",
            r.returncode == 0
            and fx.unit("firebot-media@roof_thermal") == ("active", "enabled")
            and fx.unit("firebot-media@roof_rgb") == ("inactive", "disabled"),
            r.stdout + r.stderr,
        )
        check(
            "PROTECTED_UNITS start 后 G1 不变",
            fx.unit("firebot-vehicle-agent-v1-readonly") == g1_before,
        )
        st = fx.ctl("status")
        check(
            "STATUS 双路分别可见",
            "camera=roof_rgb" in st.stdout and "camera=roof_thermal" in st.stdout,
        )
        r = fx.ctl("stop")
        check(
            "STOP all",
            r.returncode == 0 and fx.unit("firebot-media@roof_thermal") == ("inactive", "disabled"),
        )
        # -------- tamper detection
        first_sha = fx.sha
        mf = fx.edge / "media/releases" / first_sha / "firebot_media/publisher.py"
        os.chmod(mf, 0o644)
        orig = mf.read_text()
        mf.write_text(orig + "\n# tampered\n")
        r = fx.ctl("install", "--sha", first_sha, "--libsrt-prebuilt", str(fx.libsrt))
        check(
            "篡改 release → 拒绝覆盖",
            r.returncode != 0 and "RELEASE_CORRUPT_OR_MISMATCH" in r.stdout,
            r.stdout,
        )
        mf.write_text(orig)
        # -------- upgrade + rollback
        fx.commit_change()
        r = fx.ctl("install", "--sha", fx.sha, "--libsrt-prebuilt", str(fx.libsrt))
        cur = os.readlink(fx.edge / "media/current")
        prev = os.readlink(fx.edge / "media/previous")
        check(
            "UPGRADE current/previous",
            r.returncode == 0 and cur == "releases/" + fx.sha and prev == "releases/" + first_sha,
            (r.stdout + r.stderr, cur, prev),
        )
        fx.ctl("start", "all")
        r = fx.ctl("rollback")
        check(
            "ROLLBACK current←previous 且服务停止",
            r.returncode == 0
            and os.readlink(fx.edge / "media/current") == "releases/" + first_sha
            and fx.unit("firebot-media@roof_rgb")[0] == "inactive",
            r.stdout + r.stderr,
        )
        check(
            "PROTECTED_UNITS 全流程 G1 readonly agent 不变",
            fx.unit("firebot-vehicle-agent-v1-readonly") == g1_before,
        )
        r = fx.ctl("doctor")
        check("DOCTOR 只读完成", r.returncode == 0 and "MEDIA_DOCTOR=DONE" in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)
    if FAILURES:
        print("\nMEDIA_LIFECYCLE_TEST=FAIL count=%d" % len(FAILURES))
        return 1
    print("\nMEDIA_LIFECYCLE_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
