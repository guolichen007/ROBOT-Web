#!/usr/bin/env python3
"""G4-R1 D：60-onboard-vehicle.sh 媒体凭证派生——行为级反证测试（无 docker/无 root，CI 可跑）。

在临时 runtime 上真实执行脚本；PATH 前置一层 argv 记录 shim（覆盖脚本可能 exec 的
全部外部命令），断言：
  - MASTER_SECRET_ARGV：任何子进程 argv 中都不出现 master media_publish_token 原值；
  - --resync-srt / --media-only 派生的 token == HMAC-SHA256(master, "vehicle-srt:"+id)
    （与 apps/api/app/modules/media/router.py::vehicle_srt_publish_token 同算法）；
  - 凭证文件 0600、目录 0700；
  - --media-only：不生成/不改 mqtt_password，不调用 docker/sudo/mosquitto_passwd，
    不触碰 secrets/mosquitto/passwords；重复执行幂等（REUSED，内容不变）；
  - master 旋转后 --resync-srt 只改 SRT 副本，MQTT 密码字节不变；
  - 非法 vehicle_id / 未知模式 / master 缺失 → 非零退出且不落盘。
反证：--legacy <path> 对旧脚本运行同一 argv 断言，必须检出泄露（证明测试有效）。

用法：python3 scripts/test_onboard_media_credentials.py [--legacy <old-script>]
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/u-dao/scripts/60-onboard-vehicle.sh"
VEHICLE = "firebot-vehicle-01"
MASTER = "g4r1-master-media-token-ARGV-CANARY-7f3a9c"
MASTER2 = "g4r1-rotated-master-ARGV-CANARY-2b8d41"
PASSPHRASE = "g4r1-srt-passphrase-canary-000"
FAILURES: list[str] = []
# 脚本可能 exec 的外部命令（builtin 如 printf/echo/[ 不产生进程，无 argv 暴露面）
SHIMMED = (
    "python3 openssl awk cat cp mv mktemp chmod chown mkdir grep stat rm "
    "docker sudo mosquitto_passwd dirname sleep"
).split()
FORBIDDEN_IN_MEDIA_ONLY = ("docker", "sudo", "mosquitto_passwd")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def expected_token(master: str, vehicle: str = VEHICLE) -> str:
    return hmac.new(master.encode(), f"vehicle-srt:{vehicle}".encode(), hashlib.sha256).hexdigest()


def make_shims(base: Path) -> tuple[Path, Path]:
    shim_dir = base / "shim"
    shim_dir.mkdir()
    log = base / "argv.log"
    log.touch()
    real_path = os.environ.get("PATH", "/usr/bin:/bin")
    for name in SHIMMED:
        real = shutil.which(name, path=real_path)
        target = shim_dir / name
        if name in FORBIDDEN_IN_MEDIA_ONLY or real is None:
            # docker/sudo/mosquitto_passwd：只记录、拒绝执行（无真实副作用）
            body = f'printf "%s\\0" {name} "$@" >> "{log}"; printf "\\n" >> "{log}"; exit 97\n'
        else:
            body = (
                f'printf "%s\\0" {name} "$@" >> "{log}"; printf "\\n" >> "{log}"\n'
                f'exec "{real}" "$@"\n'
            )
        target.write_text("#!/bin/bash\n" + body)
        target.chmod(0o755)
    return shim_dir, log


def make_runtime(base: Path, master: str = MASTER) -> Path:
    runtime = base / "runtime"
    (runtime / "secrets/app").mkdir(parents=True)
    (runtime / "secrets/mosquitto").mkdir(parents=True)
    # 生产格式：02-gen-bootstrap-secrets.sh 用 `openssl rand -base64 N >` 落盘，末尾带换行
    (runtime / "secrets/app/media_publish_token").write_text(master + "\n")
    (runtime / "secrets/app/srt_publish_passphrase").write_text(PASSPHRASE)
    (runtime / "secrets/mosquitto/passwords").write_text("R001:$7$existing-hash\n")
    for p in (runtime / "secrets/app").iterdir():
        p.chmod(0o600)
    return runtime


def run(script: Path, runtime: Path, shim: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "U_DAO_RUNTIME_ROOT": str(runtime),
        "U_DAO_REPO_ROOT": str(ROOT),
        "HOME": str(runtime),
        "LANG": "C.UTF-8",
    }
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60
    )


def argv_records(log: Path) -> list[list[str]]:
    out = []
    for line in log.read_bytes().split(b"\n"):
        if line:
            out.append([p.decode(errors="replace") for p in line.split(b"\0") if p])
    return out


def leaked(log: Path, *secrets: str) -> list[str]:
    hits = []
    for rec in argv_records(log):
        joined = " ".join(rec)
        if any(s in joined for s in secrets):
            hits.append(rec[0])
    return hits


def mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:]


def test_media_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shim, log = make_shims(base)
        runtime = make_runtime(base)
        passwords_before = (runtime / "secrets/mosquitto/passwords").read_bytes()
        res = run(SCRIPT, runtime, shim, VEHICLE, "--media-only")
        vdir = runtime / "secrets/vehicles" / VEHICLE
        check("MEDIA_ONLY exit 0", res.returncode == 0, res.stderr[-400:])
        check("MEDIA_ONLY state=CREATED", "state=CREATED" in res.stdout, res.stdout[-300:])
        token_file = vdir / "srt_publish_token"
        check(
            "MEDIA_ONLY token == HMAC(master, vehicle-srt:id)",
            token_file.is_file() and token_file.read_text() == expected_token(MASTER),
        )
        check(
            "MEDIA_ONLY passphrase 副本一致",
            (vdir / "srt_publish_passphrase").read_text() == PASSPHRASE,
        )
        check(
            "CREDENTIAL_PERMISSION token/passphrase 0600、目录 0700",
            mode(token_file) == "600"
            and mode(vdir / "srt_publish_passphrase") == "600"
            and mode(vdir) == "700",
            f"{mode(token_file)} {mode(vdir / 'srt_publish_passphrase')} {mode(vdir)}",
        )
        check("MEDIA_ONLY 不生成 mqtt_password", not (vdir / "mqtt_password").exists())
        check(
            "MEDIA_ONLY 不触碰 mosquitto passwords",
            (runtime / "secrets/mosquitto/passwords").read_bytes() == passwords_before,
        )
        called = {rec[0] for rec in argv_records(log)}
        check(
            "MEDIA_ONLY 不调用 docker/sudo/mosquitto_passwd（不 reload Broker）",
            not (called & set(FORBIDDEN_IN_MEDIA_ONLY)),
            str(called & set(FORBIDDEN_IN_MEDIA_ONLY)),
        )
        check(
            "MASTER_SECRET_ARGV（--media-only 全部子进程 argv 无 master 原值）",
            not leaked(log, MASTER),
            str(leaked(log, MASTER)),
        )
        check(
            "MEDIA_ONLY stdout/stderr 不回显 token/master/passphrase",
            not any(
                s in res.stdout + res.stderr for s in (MASTER, PASSPHRASE, expected_token(MASTER))
            ),
        )
        check("无残留临时文件", not any(p.name.startswith(".") for p in vdir.iterdir()))
        # 幂等：再次执行 → REUSED，内容不变
        res2 = run(SCRIPT, runtime, shim, VEHICLE, "--media-only")
        check(
            "MEDIA_ONLY 幂等（REUSED，token 不变）",
            res2.returncode == 0
            and "state=REUSED" in res2.stdout
            and token_file.read_text() == expected_token(MASTER),
            res2.stderr[-300:],
        )


def test_resync_after_rotation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shim, log = make_shims(base)
        runtime = make_runtime(base)
        vdir = runtime / "secrets/vehicles" / VEHICLE
        vdir.mkdir(parents=True, mode=0o700)
        (vdir / "mqtt_password").write_text("existing-mqtt-password-must-not-change")
        (vdir / "mqtt_password").chmod(0o600)
        (vdir / "srt_publish_token").write_text(expected_token(MASTER))
        # master 旋转
        (runtime / "secrets/app/media_publish_token").write_text(MASTER2 + "\n")
        res = run(SCRIPT, runtime, shim, VEHICLE, "--resync-srt")
        check("RESYNC exit 0", res.returncode == 0, res.stderr[-400:])
        check(
            "RESYNC token == HMAC(rotated master)",
            (vdir / "srt_publish_token").read_text() == expected_token(MASTER2),
        )
        check(
            "RESYNC 不改 MQTT 密码",
            (vdir / "mqtt_password").read_text() == "existing-mqtt-password-must-not-change",
        )
        check(
            "MASTER_SECRET_ARGV（--resync-srt 全部子进程 argv 无 master 原值）",
            not leaked(log, MASTER, MASTER2),
            str(leaked(log, MASTER, MASTER2)),
        )
        called = {rec[0] for rec in argv_records(log)}
        check(
            "RESYNC 不调用 openssl/docker/sudo",
            not (called & {"openssl", "docker", "sudo"}),
            str(called),
        )
        check("RESYNC 权限 0600", mode(vdir / "srt_publish_token") == "600")


def test_negative_inputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shim, _ = make_shims(base)
        runtime = make_runtime(base)
        res = run(SCRIPT, runtime, shim, "bad/../id", "--media-only")
        check("非法 vehicle_id 拒绝", res.returncode != 0 and "非法字符" in res.stderr)
        res = run(SCRIPT, runtime, shim, VEHICLE, "--media-onyl")
        check(
            "未知模式拒绝（不再落入默认生成分支）",
            res.returncode != 0 and not (runtime / "secrets/vehicles" / VEHICLE).exists(),
            res.stderr[-200:],
        )
        (runtime / "secrets/app/media_publish_token").write_text("  \n")
        res = run(SCRIPT, runtime, shim, VEHICLE, "--media-only")
        check(
            "master 为空/仅空白 → 拒绝且不写 token",
            res.returncode != 0
            and not (runtime / "secrets/vehicles" / VEHICLE / "srt_publish_token").exists(),
            res.stderr[-200:],
        )
        res = run(SCRIPT, runtime, shim, "firebot-vehicle-02", "--resync-srt")
        check("--resync-srt 无凭据目录 → 拒绝", res.returncode != 0)


def legacy_counterproof(old_script: Path) -> int:
    """反证：旧脚本 --resync-srt 必须被本测试检出 master 出现在 argv。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shim, log = make_shims(base)
        runtime = make_runtime(base)
        (runtime / "secrets/vehicles" / VEHICLE).mkdir(parents=True)
        res = run(old_script, runtime, shim, VEHICLE, "--resync-srt")
        hits = leaked(log, MASTER)
        print(f"LEGACY rc={res.returncode} leaked_in={hits}")
        if hits:
            print("LEGACY_COUNTERPROOF=DETECTED（旧实现 master 出现在子进程 argv）")
            return 0
        print("LEGACY_COUNTERPROOF=NOT_DETECTED（测试无效）")
        return 1


# 旧 --resync-srt 的最小复刻（8c3586f 第 170–172 行原样语义）：仅作探针自检，验证 argv
# 记录器确实能检出“master 经 openssl -hmac 进入 argv”。不落盘到仓库、不用于任何部署。
LEGACY_PATTERN = r"""#!/usr/bin/env bash
set -euo pipefail
MASTER_TOKEN="$(cat "$U_DAO_RUNTIME_ROOT/secrets/app/media_publish_token")"
printf 'vehicle-srt:%s' "$1" | \
  openssl dgst -sha256 -hmac "$MASTER_TOKEN" | awk '{print $2}' >/dev/null
"""


def test_probe_detects_legacy_pattern() -> None:
    """探针自检（CI 常驻）：旧实现模式必须被 argv 记录器检出，否则本测试的 PASS 无意义。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shim, log = make_shims(base)
        runtime = make_runtime(base)
        legacy = base / "legacy.sh"
        legacy.write_text(LEGACY_PATTERN)
        res = run(legacy, runtime, shim, VEHICLE)
        hits = leaked(log, MASTER)
        check(
            "PROBE_SELFTEST 旧 openssl -hmac 模式被检出（master 出现在子进程 argv）",
            res.returncode == 0 and "openssl" in hits,
            f"rc={res.returncode} hits={hits} {res.stderr[-200:]}",
        )


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--legacy":
        return legacy_counterproof(Path(sys.argv[2]))
    test_probe_detects_legacy_pattern()
    test_media_only()
    test_resync_after_rotation()
    test_negative_inputs()
    if FAILURES:
        print(f"\nONBOARD_MEDIA_CREDENTIALS_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nONBOARD_MEDIA_CREDENTIALS_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
