#!/usr/bin/env python3
"""G4-Fleet-R2 媒体验收台（隔离环境；测试专用，绝不连接生产）。

组件（全部 127.0.0.1 随机端口，单次调用内启动/销毁）：
  - 真实 API（uvicorn app.main:app，/api/v1/media/authorize 真实鉴权）+ 隔离 PG/Redis
  - 真实 MediaMTX 1.18.2（authMethod http → API；srtPublishPassphrase 平台共享口令）
  - 车内 web_video_server 替身（MJPEG multipart，测试图案；标注 SYNTHETIC，不作为真实硬件证据）
  - Edge Home（tmp）+ 发布器（Ubuntu 20.04 同版本 python3.8 / FFmpeg 4.2.7 / libsrt 1.4.0）
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path("/sessions/confident-wonderful-pasteur/mnt/outputs")
FOCALPY = str(OUT / "env/focalpy.sh")
MEDIAMTX = str(OUT / "env/mtx/mediamtx")
MASTER = "g4fr2-isolated-media-master-0123456789abcdef"
PASSPHRASE = "g4fr2-isolated-srt-passphrase"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def vehicle_token(vehicle_id: str) -> str:
    return hmac.new(
        MASTER.encode(), ("vehicle-srt:" + vehicle_id).encode(), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------- fake web_video_server
class FakeWVS:
    """web_video_server 兼容 MJPEG 源：/stream?topic=<t>&type=mjpeg（每个 topic 一路测试图案）。

    多个 HTTP 消费者共享同一帧缓冲（模拟“单次采集、多消费者”）。可 pause(topic) 模拟相机缺失。
    """

    def __init__(self, port: int, topics: dict) -> None:
        self.port = port
        self.frames: dict = {t: None for t in topics}
        self.paused: set = set()
        self.clients: dict = {t: 0 for t in topics}
        self.served: dict = {t: 0 for t in topics}
        self.procs = []
        for topic, pattern in topics.items():
            threading.Thread(target=self._gen, args=(topic, pattern), daemon=True).start()
        wvs = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                topic = (q.get("topic") or [""])[0]
                if topic not in wvs.frames:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Connection", "close")
                self.send_header(
                    "Content-type", "multipart/x-mixed-replace;boundary=boundarydonotcross"
                )
                self.end_headers()
                wvs.clients[topic] += 1
                try:
                    self.wfile.write(b"--boundarydonotcross\r\n")
                    last = None
                    while True:
                        f = wvs.frames[topic]
                        if topic not in wvs.paused and f is not None and f is not last:
                            last = f
                            self.wfile.write(
                                b"Content-type: image/jpeg\r\nX-Timestamp: %.6f\r\n"
                                b"Content-Length: %d\r\n\r\n" % (time.time(), len(f))
                            )
                            self.wfile.write(f)
                            self.wfile.write(b"\r\n--boundarydonotcross\r\n")
                            wvs.served[topic] += 1
                        time.sleep(0.005)
                except Exception:
                    pass
                finally:
                    wvs.clients[topic] -= 1

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = S(("127.0.0.1", port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _gen(self, topic, pattern):
        p = subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                pattern,
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-q:v",
                "6",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
        )
        self.procs.append(p)
        buf = b""
        while True:
            d = p.stdout.read(65536)
            if not d:
                return
            buf += d
            while True:
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2)
                if s < 0 or e < 0:
                    break
                self.frames[topic] = buf[s : e + 2]
                buf = buf[e + 2 :]

    def close(self):
        self.server.shutdown()
        for p in self.procs:
            p.kill()


# ---------------------------------------------------------------- stack
class Stack:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.procs: list = []
        self.handles: list = []

    def spawn(self, name, args, env=None, cwd=None):
        h = open(self.workdir / ("%s.log" % name), "ab")
        self.handles.append(h)
        p = subprocess.Popen(
            args,
            cwd=str(cwd or REPO),
            env=env or dict(os.environ),
            stdout=h,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.procs.append(p)
        return p

    def start_api_mtx(self):
        self.api_port, self.srt_port, self.api_mtx, self.whep, self.rtsp = (
            free_port() for _ in range(5)
        )
        env = dict(os.environ)
        env.update(MEDIA_PUBLISH_TOKEN=MASTER, PYTHONPATH="%s:%s" % (REPO / "apps/api", REPO))
        self.api = self.spawn(
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
                str(self.api_port),
            ],
            env,
        )
        assert wait_port(self.api_port, 40), (self.workdir / "api.log").read_text()[-2000:]
        conf = self.workdir / "mediamtx.yml"
        conf.write_text(
            "\n".join(
                [
                    "logLevel: info",
                    "api: yes",
                    "apiAddress: 127.0.0.1:%d" % self.api_mtx,
                    "authMethod: http",
                    "authHTTPAddress: http://127.0.0.1:%d/api/v1/media/authorize" % self.api_port,
                    "authHTTPExclude:",
                    "  - action: api",
                    "rtsp: yes",
                    "rtspTransports: [tcp]",
                    "rtspAddress: 127.0.0.1:%d" % self.rtsp,
                    "rtmp: no",
                    "hls: no",
                    "srt: yes",
                    "srtAddress: 127.0.0.1:%d" % self.srt_port,
                    "webrtc: no",
                    "paths:",
                    "  all_others:",
                    "    srtPublishPassphrase: %s" % PASSPHRASE,
                ]
            )
            + "\n"
        )
        self.mtx = self.spawn("mediamtx", [MEDIAMTX, str(conf)])
        assert wait_port(self.api_mtx, 20)

    def paths(self) -> dict:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/v3/paths/list" % self.api_mtx, timeout=5
        ) as r:
            data = json.load(r)
        return {i["name"]: i for i in data.get("items", [])}

    def stop_proc(self, p, timeout=10):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def close(self):
        for p in reversed(self.procs):
            try:
                os.killpg(p.pid, 9)
            except Exception:
                pass
        for h in self.handles:
            h.close()


def make_edge_home(
    root: Path,
    vehicle_id: str,
    srt_port: int,
    wvs_port: int,
    *,
    cameras="roof_rgb,roof_thermal",
    token: str | None = None,
    passphrase: str = PASSPHRASE,
    extra: str = "",
) -> Path:
    home = root / ("edge-%s" % vehicle_id)
    (home / "config").mkdir(parents=True, exist_ok=True)
    sec = home / "media" / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    os.chmod(sec, 0o700)
    hw = (
        REPO / "integration/ros1/vehicle-edge/media/profiles/firebot_ros1_standard_v1.media.env"
    ).read_text()
    hw = hw.replace("127.0.0.1:8080", "127.0.0.1:%d" % wvs_port)
    (home / "config/media-hardware.env").write_text(hw)
    (home / "config/media.env").write_text(
        "FIREBOT_MEDIA_VEHICLE_ID=%s\nFIREBOT_MEDIA_SRT_HOST=127.0.0.1\nFIREBOT_MEDIA_SRT_PORT=%d\n"
        "FIREBOT_MEDIA_CAMERAS=%s\nFIREBOT_MEDIA_ENCODER=libx264\nFIREBOT_MEDIA_BACKOFF_INITIAL_S=1\n"
        "FIREBOT_MEDIA_BACKOFF_MAX_S=4\nFIREBOT_MEDIA_AUTH_BACKOFF_S=3\n%s"
        % (vehicle_id, srt_port, cameras, extra)
    )
    for name, value in (
        ("srt_publish_token", token if token is not None else vehicle_token(vehicle_id)),
        ("srt_publish_passphrase", passphrase),
    ):
        p = sec / name
        p.write_text(value + "\n")
        os.chmod(p, 0o600)
    return home


def register_streams(vehicle_id: str, cameras=("roof_rgb", "roof_thermal"), create_robot=True):
    """隔离测试库：确保测试车辆资产 + 真实（非 SYNTHETIC）流登记。返回需清理的 id。"""
    sys.path.insert(0, str(REPO / "apps/api"))
    from app.db.models import Robot, Site, StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import select

    created = {"robot": None, "streams": []}
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        if robot is None and create_robot:
            site = db.scalar(select(Site).order_by(Site.id).limit(1))
            robot = Robot(vehicle_id=vehicle_id, name=vehicle_id, site_id=site.id, enabled=False)
            db.add(robot)
            db.flush()
            created["robot"] = robot.id
        for cam in cameras:
            sid = "%s-%s" % (vehicle_id, cam)
            if not db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == sid)):
                db.add(
                    StreamRegistry(
                        stream_id=sid,
                        robot_id=robot.id,
                        camera_type=cam,
                        source_ref="REAL_VEHICLE_SRT",
                        state="OFFLINE",
                        playback_url="/media/%s/whep" % sid,
                    )
                )
                created["streams"].append(sid)
        db.commit()
    return created


def cleanup_streams(created):
    from app.db.models import Base, Robot, StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import delete

    with SessionLocal() as db:
        if created["streams"]:
            db.execute(
                delete(StreamRegistry).where(StreamRegistry.stream_id.in_(created["streams"]))
            )
        if created["robot"]:
            for t in reversed(Base.metadata.sorted_tables):
                if "robot_id" in t.c and t.name != "robots":
                    db.execute(t.delete().where(t.c.robot_id == created["robot"]))
            db.execute(delete(Robot).where(Robot.id == created["robot"]))
        db.commit()


def publisher_cmd(home: Path, camera: str):
    return [FOCALPY, "-m", "firebot_media", camera, "--edge-home", str(home)]


def read_state(home: Path, camera: str) -> dict:
    try:
        return json.loads((home / ("media/state/%s.json" % camera)).read_text())
    except (OSError, ValueError):
        return {}


def wait_state(home, camera, states, timeout=30.0):
    end = time.monotonic() + timeout
    st = {}
    while time.monotonic() < end:
        st = read_state(home, camera)
        if st.get("state") in states:
            return st
        time.sleep(0.3)
    return st


def wait_ready(stack: Stack, path: str, timeout=30.0, want=True):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        p = stack.paths().get(path)
        ok = bool(p and p.get("ready"))
        if ok == want:
            return p
        time.sleep(0.3)
    return stack.paths().get(path)


def tempdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="g4fr2-", dir="/tmp"))


__all__ = [n for n in dir() if not n.startswith("_")]
_ = shutil
