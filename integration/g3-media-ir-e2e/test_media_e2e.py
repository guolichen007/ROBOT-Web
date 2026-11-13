"""MEDIA_RGB_E2E / MEDIA_THERMAL_E2E / MEDIA_AUTH_ISOLATION（真实 MediaMTX + 真实 API 鉴权）。

拓扑：uvicorn(app.main) ← MediaMTX authHTTP；
services/media-test-source（现有合成源，SIMULATED 叠字）
经 SRT 发布到 MediaMTX；播放侧：/api/v1/media/tickets → WHEP（Authorization: Bearer ticket）、
以及 RTSP 读取（ffprobe，携带票据）。不新建媒体服务器。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest
from conftest import REPO_ROOT, free_port, mediamtx_binary, spawn, stop, wait_for, wait_port

SIM_MEDIA = "firebot-sim-media-01"
RGB = f"{SIM_MEDIA}-roof_rgb"
THERMAL = f"{SIM_MEDIA}-roof_thermal"
MASTER = "g3-media-master-token-isolated-0123456789"
PASSPHRASE = "g3-srt-passphrase-isolated-01"
EVIDENCE: dict = {}


@pytest.fixture(scope="module")
def media(g3):
    binary = mediamtx_binary()
    if not binary or not subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0:
        pytest.skip("MediaMTX/ffmpeg 不可用：MEDIA E2E NOT_EXECUTED")
    workdir = g3["workdir"]
    os.environ["MEDIA_PUBLISH_TOKEN"] = MASTER
    os.environ["SYNTHETIC_MEDIA_VEHICLE_IDS"] = SIM_MEDIA
    from app.core.config import get_settings

    get_settings.cache_clear()
    _register_streams()
    api_port, rtsp, srt, whep, ctl = (free_port() for _ in range(5))
    env = {**g3["base"], "MEDIA_PUBLISH_TOKEN": MASTER, "SYNTHETIC_MEDIA_VEHICLE_IDS": SIM_MEDIA}
    master_file = workdir / "media-master"
    master_file.write_text(MASTER)
    master_file.chmod(0o600)
    api = spawn(
        workdir,
        "api",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            "apps/api",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ],
        env,
    )
    assert wait_port(api_port, 30), (workdir / "api.log").read_text()[-1500:]
    conf = workdir / "mediamtx.yml"
    conf.write_text(
        "\n".join(
            [
                "logLevel: info",
                "api: yes",
                f"apiAddress: 127.0.0.1:{ctl}",
                "authMethod: http",
                f"authHTTPAddress: http://127.0.0.1:{api_port}/api/v1/media/authorize",
                "authHTTPExclude:",
                "  - action: api",
                "rtsp: yes",
                f"rtspAddress: 127.0.0.1:{rtsp}",
                "rtmp: no",
                "hls: no",
                "srt: yes",
                f"srtAddress: 127.0.0.1:{srt}",
                "webrtc: yes",
                f"webrtcAddress: 127.0.0.1:{whep}",
                "webrtcLocalUDPAddress: 127.0.0.1:0",
                "webrtcAllowOrigins: ['*']",
                "paths:",
                "  all_others:",
                f"    srtPublishPassphrase: {PASSPHRASE}",
            ]
        )
        + "\n"
    )
    mtx = spawn(workdir, "mediamtx", [binary, str(conf)], dict(os.environ))
    assert wait_port(ctl, 15) and wait_port(rtsp, 15)
    context = {
        "api_port": api_port,
        "rtsp": rtsp,
        "srt": srt,
        "whep": whep,
        "ctl": ctl,
        "workdir": workdir,
        "base": env,
        "publishers": {},
        "master_file": master_file,
    }
    try:
        yield context
    finally:
        for proc in context["publishers"].values():
            stop(proc)
        stop(mtx)
        stop(api)
        _unregister_streams()


def _register_streams():
    from app.db.models import Robot, Site, StreamRegistry
    from app.db.session import SessionLocal
    from app.modules.media.router import SYNTHETIC_TEST_SOURCE
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM_MEDIA))
        if robot is None:
            site = db.scalar(select(Site).order_by(Site.id).limit(1))
            robot = Robot(
                vehicle_id=SIM_MEDIA, name="SIMULATED MEDIA 01", site_id=site.id, enabled=False
            )
            db.add(robot)
            db.flush()
        for stream_id, camera in ((RGB, "roof_rgb"), (THERMAL, "roof_thermal")):
            if not db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id)):
                db.add(
                    StreamRegistry(
                        stream_id=stream_id,
                        robot_id=robot.id,
                        camera_type=camera,
                        source_ref=SYNTHETIC_TEST_SOURCE,
                        state="OFFLINE",
                        playback_url=f"/media/{stream_id}/whep",
                    )
                )
        db.commit()


def _unregister_streams():
    from app.db.models import Robot, StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM_MEDIA))
        db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id.in_([RGB, THERMAL])))
        if robot:
            db.delete(robot)
        db.commit()


def start_publisher(media, stream_id, *, kind, user="", name=None):
    """合成源只拿到主密钥文件路径（0600）；在进程内派生按流 token，主密钥不进入 ffmpeg argv。"""
    env = {
        key: value
        for key, value in media["base"].items()
        if key not in {"MEDIA_PUBLISH_TOKEN", "MEDIA_PUBLISH_MASTER"}
    }
    env.update(
        {
            "MEDIA_STREAM_ID": stream_id,
            "OUTPUT_PROTOCOL": "srt",
            "SRT_USER": user,
            "MEDIA_PUBLISH_MASTER_FILE": str(media["master_file"]),
            "SRT_PUBLISH_PASSPHRASE": PASSPHRASE,
            "MEDIA_SRT_ORIGIN": f"srt://127.0.0.1:{media['srt']}",
            "SIMULATED_LABEL": f"{kind.upper()} {stream_id}",
            "SIMULATED_KIND": kind,
            "HEALTH_PORT": str(free_port()),
            "HEALTH_HOST": "127.0.0.1",
        }
    )
    proc = spawn(
        media["workdir"],
        name or f"pub-{stream_id}",
        ["node", "services/media-test-source/main.mjs"],
        env,
    )
    media["publishers"][name or stream_id] = proc
    return proc


def cmdlines() -> list[str]:
    result = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    result.append(handle.read().replace(b"\0", b" ").decode(errors="replace"))
            except OSError:
                continue
    return result


def path_ready(media, stream_id):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{media['ctl']}/v3/paths/get/{stream_id}", timeout=3
        ) as r:
            item = json.loads(r.read())
    except (urllib.error.URLError, OSError):
        return None
    return item if item.get("ready") else None


def admin_api(api_port):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "g2c", REPO_ROOT / "integration" / "g2-command-loop-e2e" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    username, password = module.ensure_isolated_test_admin()
    req = urllib.request.Request(
        f"http://127.0.0.1:{api_port}/api/v1/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def ticket_for(media, stream_id):
    token = admin_api(media["api_port"])
    req = urllib.request.Request(
        f"http://127.0.0.1:{media['api_port']}/api/v1/media/tickets",
        data=json.dumps({"stream_id": stream_id}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["ticket"]


OFFER = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 127.0.0.1\r\n"
    "a=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;profile-level-id=42e01f\r\n"
    "a=setup:actpass\r\na=mid:0\r\na=ice-ufrag:aaaaaaaa\r\na=ice-pwd:aaaaaaaaaaaaaaaaaaaaaaaa\r\n"
    "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:"
    "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\na=recvonly\r\n"
)


def whep(media, stream_id, ticket):
    req = urllib.request.Request(
        f"http://127.0.0.1:{media['whep']}/{stream_id}/whep",
        data=OFFER.encode(),
        headers={
            "Content-Type": "application/sdp",
            **({"Authorization": f"Bearer {ticket}"} if ticket else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Location"), r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode()


def whep_delete(media, location):
    req = urllib.request.Request(f"http://127.0.0.1:{media['whep']}{location}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


@pytest.mark.parametrize(("stream_id", "kind"), [(RGB, "rgb"), (THERMAL, "thermal")])
def test_publish_play_and_reconnect(media, stream_id, kind):
    start_publisher(media, stream_id, kind=kind)
    item = wait_for(lambda: path_ready(media, stream_id), 25)
    # 凭证边界：主密钥绝不出现在任何进程 argv（ffmpeg 只拿按流派生口令）
    procs = cmdlines()
    assert any("ffmpeg" in c and stream_id in c for c in procs)
    assert not [c for c in procs if MASTER in c]
    assert item, (media["workdir"] / f"pub-{stream_id}.log").read_text()[-800:]
    tracks = item.get("tracks") or item.get("tracks2") or []
    assert any("H264" in json.dumps(t) for t in tracks), tracks
    ticket = ticket_for(media, stream_id)
    status, location, answer = whep(media, stream_id, ticket)
    assert status == 201 and location and "m=video" in answer, (status, answer[:200])
    assert whep_delete(media, location) == 200  # Web「断开视频」= DELETE WHEP 会话
    # 断流：停止发布进程 → 路径不再 ready；重启发布 → 恢复 ready 并可再次协商
    stop(media["publishers"].pop(stream_id))
    assert wait_for(lambda: path_ready(media, stream_id) is None, 15)
    status, _, _ = whep(media, stream_id, ticket_for(media, stream_id))
    assert status == 404  # 无源时播放端拿不到会话（不伪造在线）
    start_publisher(media, stream_id, kind=kind)
    assert wait_for(lambda: path_ready(media, stream_id), 25)
    status, location, _ = whep(media, stream_id, ticket_for(media, stream_id))
    assert status == 201
    whep_delete(media, location)
    EVIDENCE[stream_id] = {"whep": 201, "disconnect_delete": 200, "reconnect": True}


def test_simulated_watermark_in_frame(media):
    """SIMULATED 标志：用 media-test-source 的同一叠字滤镜链（overlayArgs，与发布进程相同参数）
    渲染一帧，校验顶部红色横幅并保存 PNG；MediaMTX 不转码（H.264 透传），播放端看到的即此画面。

    说明：沙箱无 WebRTC 客户端可解码 WHEP 媒体面；ffmpeg 的 RTSP basic 口令 120 字符截断
    无法携带 JWT 票据（实测），故帧级断言在发布端滤镜链上完成，WHEP 协商由上例覆盖。
    """
    import re

    source = (REPO_ROOT / "services/media-test-source/main.mjs").read_text(encoding="utf-8")
    assert "SIMULATED ${safe}" in source and "pseudocolor" in source
    for stream_id, kind in ((RGB, "rgb"), (THERMAL, "thermal")):
        script = (
            "process.env.SIMULATED_LABEL=process.argv[1];process.env.SIMULATED_KIND=process.argv[2];"
            "const src=require('fs').readFileSync(process.argv[3],'utf8');"
            "const fn=src.slice(src.indexOf('function overlayArgs'),src.indexOf('const args = ['));"
            "console.log(JSON.stringify(eval('('+fn+')')()))"
        )
        out = subprocess.run(
            [
                "node",
                "-e",
                script,
                kind.upper(),
                kind,
                str(REPO_ROOT / "services/media-test-source/main.mjs"),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        overlay = json.loads(out.stdout)
        assert overlay[0] == "-vf" and re.search(r"SIMULATED (RGB|THERMAL)", overlay[1])
        png = media["workdir"] / f"{stream_id}.png"
        common = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=15",
        ]
        subprocess.run(
            [*common, *overlay, "-frames:v", "1", "-y", str(png)],
            capture_output=True,
            timeout=30,
            check=True,
        )
        crop = overlay[1] + ",crop=iw:40:0:0,scale=1:1"
        pixel = subprocess.run(
            [*common, "-vf", crop, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout
        r, g, b = pixel[:3]
        assert r > 150 and g < 90 and b < 90, (stream_id, r, g, b)  # 红色 SIMULATED 横幅
        EVIDENCE.setdefault(stream_id, {})["banner_rgb"] = [r, g, b]
        EVIDENCE[stream_id]["frame_png"] = png.name


def test_publish_auth_isolation_real_mediamtx(media):
    """真实 MediaMTX → authHTTP：车辆凭证不能发布合成流；错误 token 被拒；跨流票据播放被拒。"""
    from app.modules.media.router import vehicle_srt_publish_token

    os.environ["MEDIA_PUBLISH_TOKEN"] = MASTER
    target = f"srt://127.0.0.1:{media['srt']}"

    def raw_publish(stream_id, user, password):
        streamid = f"publish:{stream_id}:{user}:{password}"
        url = f"{target}?streamid={streamid}&pkt_size=1316&passphrase={PASSPHRASE}"
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x120:rate=5",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "mpegts",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode

    # 全局合成 token 发布车辆流 R001-roof_rgb → 拒绝（旧实现允许）
    assert raw_publish("R001-roof_rgb", "", MASTER) != 0
    # 原始主密钥也不能发布合成流（只认按流派生 token）
    assert raw_publish(RGB + "-raw", "", MASTER) != 0
    assert path_ready(media, "R001-roof_rgb") is None
    # R001 车辆凭证发布 R002 / 合成流 → 拒绝
    r001 = vehicle_srt_publish_token("R001")
    assert raw_publish("R002-roof_rgb", "R001", r001) != 0
    # 错误 token 发布合成流 → 拒绝
    assert raw_publish(RGB + "x", "", "WRONG") != 0
    # 正确车辆身份发布本车流 → 允许（真实 MediaMTX 端到端）
    assert raw_publish("R001-roof_thermal", "R001", r001) == 0
    # 播放：RGB 票据不能读 THERMAL；无票据 / 伪造票据拒绝
    rgb_ticket = ticket_for(media, RGB)
    if path_ready(media, THERMAL) is None:
        start_publisher(media, THERMAL, kind="thermal", name="thermal-auth")
        wait_for(lambda: path_ready(media, THERMAL), 25)
    assert whep(media, THERMAL, rgb_ticket)[0] in (401, 403)
    assert whep(media, THERMAL, None)[0] in (401, 403)
    assert whep(media, THERMAL, "x.y.z")[0] in (401, 403)
    logs = "".join(p.read_text(errors="replace") for p in media["workdir"].glob("*.log"))
    for secret in (MASTER, r001, PASSPHRASE):
        assert secret not in (media["workdir"] / "api.log").read_text(errors="replace")
    EVIDENCE["auth_denied_logged"] = "media publish denied" in logs
    print("G3_MEDIA_EVIDENCE " + json.dumps(EVIDENCE))


@pytest.mark.parametrize(("stream_id", "kind"), [(RGB, "rgb"), (THERMAL, "thermal")])
def test_browser_webrtc_decode(media, stream_id, kind):
    """浏览器级：真实 Chromium WebRTC → WHEP；分级断言 201 协商 / ICE connected / 解码且帧变化。

    缺少 chrome-headless-shell（CHROMIUM_PATH）时显式 SKIP——MEDIA_BROWSER_DECODE=NOT_RUN，不伪造。
    """
    chromium = os.environ.get("CHROMIUM_PATH")
    if not chromium or not os.path.exists(chromium):
        pytest.skip("CHROMIUM_PATH 未提供：浏览器解码 NOT_RUN")
    if path_ready(media, stream_id) is None:
        start_publisher(media, stream_id, kind=kind, name=f"browser-{stream_id}")
        assert wait_for(lambda: path_ready(media, stream_id), 25)
    ticket = ticket_for(media, stream_id)
    cmdlines_seen: list[str] = []
    import threading

    stop_sampling = threading.Event()

    def sample():
        while not stop_sampling.is_set():
            cmdlines_seen.extend(c for c in cmdlines() if "whep_decode" in c or "chrome" in c)
            stop_sampling.wait(0.5)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    result = subprocess.run(
        [
            "node",
            str(REPO_ROOT / "integration/g3-media-ir-e2e/browser/whep_decode.mjs"),
            f"http://127.0.0.1:{media['whep']}/{stream_id}/whep",
            "6",
        ],
        input=ticket,  # 票据经 stdin，不进 argv
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "CHROMIUM_PATH": chromium,
            "SCREENSHOT_PATH": str(media["workdir"] / f"browser-{stream_id}.png"),
        },
    )
    stop_sampling.set()
    sampler.join(2)
    assert result.returncode == 0, result.stderr[-800:]
    assert any("whep_decode" in c for c in cmdlines_seen)  # 确实采样到了解码进程
    data = json.loads(result.stdout.strip().splitlines()[-1])
    # 票据未出现在任何进程 argv 中（解码进程运行期间采样到的 cmdline 见 EVIDENCE）
    assert ticket not in json.dumps(cmdlines_seen)
    EVIDENCE.setdefault("browser", {})[stream_id] = {
        k: data.get(k)
        for k in (
            "whep_status",
            "ice_connected",
            "frames",
            "width",
            "height",
            "distinct_frames",
            "decoded_changing",
        )
    }
    EVIDENCE["browser"][stream_id]["banner_top_rgb"] = (data.get("hashes") or [{}])[-1].get("top")
    EVIDENCE["browser"][stream_id]["screenshot_after_verdict"] = data.get("screenshot") is True
    EVIDENCE["browser"][stream_id]["ticket_in_argv"] = False
    EVIDENCE["browser"][stream_id]["page"] = "isolated test page (about:blank + RTCPeerConnection)"
    print("G3_BROWSER_EVIDENCE " + json.dumps({stream_id: EVIDENCE["browser"][stream_id]}))
    assert data["whep_status"] == 201
    assert data["ice_connected"] is True
    assert data["decoded_changing"] is True, data
    r, g, b = data["hashes"][-1]["top"]
    assert r > 150 and g < 90 and b < 90  # 浏览器解码画面顶部即红色 SIMULATED 横幅
