#!/usr/bin/env python3
"""G4-Fleet-R2：vehicle enroll 的 Transport Profile / token 安全输入 / 媒体下发 行为测试。

fixture：临时 Edge Home + fake curl（记录 argv 与 stdin）+ fake systemctl（Bridge/Edge inactive）。
覆盖：
  TOKEN_ARGV_REJECTED      --token <值> 直接拒绝，零 POST
  TOKEN_FILE_PERM          token 文件 0644 拒绝
  TOKEN_NOT_IN_ARGV        --token-file / --token-stdin：curl 进程参数不含 token（经 stdin 请求体）
  TRANSPORT_REQUIRED       响应缺 transport_profile/protocol/mqtt_port → FAIL，零写入（无 8883 默认）
  PROTOCOL_NOT_EDGE        protocol=vehicle-agent-v1（001 r001-independent-v1）→ REJECTED，零写入
  ASSIGNMENT_FROM_PROFILE  assignment.env / bridge.env 端口 = Profile 端口（8884 验证非默认）
  MEDIA_BUNDLE             响应含 media → secrets/media-bundle.json 0600；主密钥字段 → 拒绝
运行：python3 tests/test_enroll_transport.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1]
ENROLL = EDGE / "lifecycle" / "enroll.sh"
TOKEN = "ab" * 32
FAILURES: list = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)[:300]))
    if not cond:
        FAILURES.append(name)


def base_resp(**over):
    d = {"device_id": "firebot-vehicle-03", "profile_id": "firebot_ros1_standard_v1",
         "transport_profile": "u-dao-standard-v1", "protocol": "canonical-mqtt-1.3",
         "mqtt_host": "firebot.youdaozd.com", "mqtt_port": 8884, "mqtt_password": "pw-x",
         "device_token": "dt-y",
         "media": {"kind": "firebot.media-credentials.v1", "vehicle_id": "firebot-vehicle-03",
                   "srt_host": "firebot.youdaozd.com", "srt_port": 8890,
                   "srt_publish_token": "d" * 64, "srt_publish_passphrase": "pp-platform-shared"}}
    for k, v in over.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return d


class Fx:
    def __init__(self, resp: dict) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="enroll-tp-"))
        self.home = self.tmp / "edge"
        (self.home / "certs").mkdir(parents=True)
        (self.home / "certs" / "mqtt-ca.crt").write_text("CA\n")
        (self.home / "config").mkdir()
        (self.home / "config" / "bootstrap.env").write_text("FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n")
        self.log = self.tmp / "curl.log"
        resp_file = self.tmp / "resp.json"
        resp_file.write_text(json.dumps(resp))
        curl = self.tmp / "curl"
        curl.write_text("#!/bin/bash\nprintf 'ARGV %%s\\n' \"$*\" >> %s\nprintf 'STDIN %%s\\n' \"$(cat)\" >> %s\n"
                        "cat %s\n" % (self.log, self.log, resp_file))
        curl.chmod(0o755)
        sysctl = self.tmp / "systemctl"
        sysctl.write_text("#!/bin/bash\necho inactive\nexit 3\n")  # 真实 systemd 语义
        sysctl.chmod(0o755)
        self.env = dict(os.environ, FIREBOT_EDGE_HOME=str(self.home), FIREBOT_TEST_CURL=str(curl),
                        FIREBOT_TEST_SYSTEMCTL=str(sysctl), FIREBOT_TEST_SKIP_ROOT="1")
        self.tok = self.tmp / "token"
        self.tok.write_text(TOKEN + "\n")
        self.tok.chmod(0o600)

    def run(self, *args, stdin=None):
        return subprocess.run(["bash", str(ENROLL), "firebot-vehicle-03", *args], input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=60)

    def wrote_anything(self):
        return any((self.home / p).exists() for p in ("config/identity.env", "config/assignment.env",
                                                      "secrets/mqtt.env", "secrets/device-token",
                                                      "secrets/media-bundle.json", "config/bridge.env"))

    def curl_argv(self):
        return [ln for ln in (self.log.read_text().splitlines() if self.log.exists() else []) if ln.startswith("ARGV")]


def main() -> int:
    fx = Fx(base_resp())
    r = fx.run("--token", TOKEN)
    check("TOKEN_ARGV_REJECTED", r.returncode != 0 and "TOKEN_ON_ARGV_FORBIDDEN" in r.stderr and not fx.curl_argv())
    fx.tok.chmod(0o644)
    r = fx.run("--token-file", str(fx.tok))
    check("TOKEN_FILE_PERM 0644 拒绝", r.returncode != 0 and not fx.curl_argv())
    fx.tok.chmod(0o600)

    r = fx.run("--token-file", str(fx.tok), "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
    check("ENROLL token-file PASS", r.returncode == 0 and "ENROLL=PASS" in r.stdout, r.stdout + r.stderr)
    check("TOKEN_NOT_IN_ARGV（curl argv 无 token；经 stdin 请求体）",
          fx.curl_argv() and not any(TOKEN in a for a in fx.curl_argv()) and TOKEN in fx.log.read_text())
    check("TOKEN 不出现在 enroll 输出", TOKEN not in r.stdout + r.stderr)
    a = (fx.home / "config/assignment.env").read_text()
    b = (fx.home / "config/bridge.env").read_text()
    check("ASSIGNMENT_FROM_PROFILE（端口=Profile 8884，非默认 8883）",
          "MQTT_PORT=8884" in a and "TRANSPORT_PROFILE=u-dao-standard-v1" in a and "FIREBOT_MQTT_PORT=8884" in b)
    mb = fx.home / "secrets/media-bundle.json"
    check("MEDIA_BUNDLE 0600 写入", mb.is_file() and stat.S_IMODE(mb.stat().st_mode) == 0o600
          and json.loads(mb.read_text())["vehicle_id"] == "firebot-vehicle-03")

    fx = Fx(base_resp())
    r = fx.run("--token-stdin", stdin=TOKEN + "\n")
    check("ENROLL token-stdin PASS 且 argv 无 token", r.returncode == 0
          and not any(TOKEN in x for x in fx.curl_argv()), r.stdout + r.stderr)

    for missing in ("transport_profile", "protocol", "mqtt_port"):
        fx = Fx(base_resp(**{missing: None}))
        r = fx.run("--token-file", str(fx.tok))
        check("TRANSPORT_REQUIRED 缺 %s → FAIL 零写入" % missing,
              r.returncode != 0 and not fx.wrote_anything() and "8883" not in r.stdout, r.stdout + r.stderr)

    fx = Fx(base_resp(protocol="vehicle-agent-v1", transport_profile="r001-independent-v1"))
    r = fx.run("--token-file", str(fx.tok))
    check("PROTOCOL_NOT_EDGE（001 r001-independent-v1）→ REJECTED 零写入",
          r.returncode != 0 and "TRANSPORT_PROTOCOL_NOT_EDGE" in r.stderr and not fx.wrote_anything(),
          r.stdout + r.stderr)

    media = base_resp()["media"]
    media["media_publish_token"] = "MASTER"
    fx = Fx(base_resp(media=media))
    r = fx.run("--token-file", str(fx.tok))
    check("MEDIA_BUNDLE 含主密钥字段 → 拒绝", r.returncode != 0 and not (fx.home / "secrets/media-bundle.json").exists())

    if FAILURES:
        print("\nENROLL_TRANSPORT_TEST=FAIL count=%d" % len(FAILURES))
        return 1
    print("\nENROLL_TRANSPORT_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
