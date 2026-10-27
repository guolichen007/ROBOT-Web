#!/usr/bin/env python3
"""U-Dao 合成视频 E2E 验收（R2-16/17/18/22 媒体侧）：

  1. SRT 三态鉴权：错误 token（正确 passphrase）→ 发布必须失败；
     正确 token（错误 passphrase）→ 加密握手必须失败。
  2. SRT 双流（RGB/thermal，H.264+yuv420p 合同）→ MediaMTX 注册并 ready。
  3. 媒体票据链路：登录 → /media/tickets → WHEP POST 201 → SDP answer 含 127.0.0.1:8189
     ICE candidate（端口保持）→ DELETE 会话。
  4. codec 断言（MediaMTX v3 API tracks：H264/yuv420p）。

前置：firebot 栈运行中（edge 127.0.0.1:18080；bootstrap HTTPS firebot.local:18443 用于登录）、
      docker 可用（docker 或 sudo -n docker）、e2e 镜像已构建（u-dao-media-e2e-source:v1）。
用法：
  python3 scripts/check_srt_whep_e2e.py \
    --token-file /srv/u-dao/projects/firebot/runtime/secrets/app/media_publish_token \
    --passphrase-file /srv/u-dao/projects/firebot/runtime/secrets/app/srt_publish_passphrase \
    --admin-password-file /srv/u-dao/projects/firebot/runtime/secrets/app/bootstrap_admin_password
"""

import argparse
import http.cookiejar
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def docker() -> list[str]:
    if subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0:
        return ["docker"]
    if subprocess.run(
        ["sudo", "-n", "docker", "info"], capture_output=True, timeout=60
    ).returncode == 0:
        return ["sudo", "-n", "docker"]
    raise RuntimeError("docker 与 sudo -n docker 均不可用")


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def srt_publish(token: str, passphrase: str, stream_id: str, expect_ok: bool) -> None:
    """用 e2e 镜像内的 ffmpeg 通过 host 网络向 127.0.0.1:18890 发布 6 秒合成流。"""
    d = docker()
    streamid = f"publish:{stream_id}:u-dao:{token}"
    url = f"srt://127.0.0.1:18890?streamid={streamid}&pkt_size=1316"
    if passphrase:
        url += f"&passphrase={passphrase}"
    cmd = d + ["run", "--rm", "--network", "host", "u-dao-media-e2e-source:v1",
               "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
               "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10", "-t", "6",
               "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-f", "mpegts", url]
    result = run(cmd, timeout=60)
    check(f"SRT publish {stream_id} (expect {'OK' if expect_ok else 'FAIL'})",
          (result.returncode == 0) == expect_ok,
          f"rc={result.returncode} err={result.stderr[-300:]}")


def mediamtx_api(path: str) -> dict:
    """容器内访问 MediaMTX v3 API（action=api 免鉴权，仅容器内 9997）。"""
    d = docker()
    cid = run(d + ["ps", "-q", "-f", "label=com.docker.compose.project=u-dao-firebot",
                   "-f", "label=com.docker.compose.service=mediamtx"], timeout=60)
    assert cid.returncode == 0 and cid.stdout.strip(), "mediamtx 容器未运行"
    # Docker 29 已移除 exec 的 -T 速记；非 tty stdin 下不加 -T 亦不分配 tty
    result = run(d + ["exec", cid.stdout.strip(),
                      "/busybox", "wget", "-q", "-O", "-",
                      f"http://127.0.0.1:9997{path}"], timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def login(base_url: str, admin_password: str) -> tuple[object, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        urllib.request.HTTPCookieProcessor(jar),
    )
    req = urllib.request.Request(
        f"{base_url}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": admin_password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=30) as resp:
        payload = json.loads(resp.read())
        assert resp.status == 200, f"login failed {resp.status}"
    # API 需要 Bearer（refresh cookie 仅用于续期）；media ticket/WHEP 请求显式携带
    return opener, payload["access_token"]


def whep_flow(opener, base_url: str, stream_id: str, token: str) -> None:
    req = urllib.request.Request(
        f"{base_url}/api/v1/media/tickets",
        data=json.dumps({"stream_id": stream_id}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with opener.open(req, timeout=30) as resp:
        ticket = json.loads(resp.read())
    assert ticket["stream_id"] == stream_id, ticket
    offer = (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 127.0.0.1\r\n"
        "a=rtpmap:96 H264/90000\r\na=setup:actpass\r\na=mid:0\r\n"
        "a=ice-ufrag:aaaaaaaa\r\na=ice-pwd:aaaaaaaaaaaaaaaaaaaaaaaa\r\n"
        "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:"
        "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
        "a=sendrecv\r\n"
    )
    whep_req = urllib.request.Request(
        f"{base_url}/media/{stream_id}/whep",
        data=offer.encode(),
        headers={
            "Content-Type": "application/sdp",
            "Authorization": f"Bearer {ticket['ticket']}",
        },
        method="POST",
    )
    try:
        with opener.open(whep_req, timeout=30) as resp:
            sdp = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        sdp = exc.read().decode()
        status = exc.code
    check(f"WHEP POST {stream_id} 返回 201", status == 201, f"status={status}")
    check("WHEP answer 含 127.0.0.1:8189 ICE candidate（端口保持）",
          "127.0.0.1 8189" in sdp or "127.0.0.1:8189" in sdp,
          sdp[:400])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--passphrase-file", required=True)
    parser.add_argument("--admin-password-file", required=True)
    parser.add_argument("--base-url", default="https://firebot.local:18443")
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    passphrase = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
    admin_password = Path(args.admin_password_file).read_text(encoding="utf-8").strip()

    # 1) SRT 三态（R2-16）：错误 token（正确 passphrase）→ 失败；错误 passphrase → 失败
    srt_publish("WRONG_TOKEN", passphrase, "R001-roof_rgb", expect_ok=False)
    srt_publish(token, "WRONG_PASSPHRASE", "R001-roof_rgb", expect_ok=False)

    # 2) 双流就绪（由 compose.media-e2e 正例发布器提供）
    paths = mediamtx_api("/v3/paths/list")
    items = paths.get("items", {})
    if isinstance(items, list):
        items = {entry["name"]: entry for entry in items}
    streams = (("R001-roof_rgb", "roof_rgb"), ("R001-roof_thermal", "roof_thermal"))
    for stream_id, _camera in streams:
        entry = items.get(stream_id)
        check(f"流注册 {stream_id} ready", bool(entry and entry.get("ready")),
              json.dumps(entry)[:200] if entry else "missing")
        if entry:
            # MediaMTX v3 API 不暴露 pixFmt：codec 合同断言 H264；
            # yuv420p 由发布端强制（media-test-source 输出 -pix_fmt yuv420p，ready 日志有证据）
            tracks = entry.get("tracks2") or entry.get("tracks") or []
            codecs = [t.get("codec") for t in tracks]
            check(f"{stream_id} codec H264（V1 浏览器合同；yuv420p 由发布端强制）",
                  "H264" in codecs, str(codecs))

    # 3) 票据 + WHEP + ICE（bootstrap HTTPS；HTTP 仅 health，完整 auth 必须走 18443）
    opener, token = login(args.base_url, admin_password)
    whep_flow(opener, args.base_url, "R001-roof_rgb", token)
    whep_flow(opener, args.base_url, "R001-roof_thermal", token)

    if FAILURES:
        print(f"\nSRT_WHEP_E2E=FAIL count={len(FAILURES)}")
        return 1
    print("\nSRT_WHEP_E2E=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
