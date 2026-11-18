#!/usr/bin/env python3
"""G4-Fleet-R2 标准化接入验收（隔离环境，单次运行 ≤ 170s；测试专用，绝不连接生产）。

两个实例，同一套实现、无车辆分支：
  NEW   firebot-vehicle-03：预登记 → 凭证准备（fleet register，批准 Profile u-dao-standard-v1）→
        enroll（token 文件，经正式 enrollment API）→ 精确 SHA 安装（edge-install + media install）→
        只读启动（readonly-start：用下发凭证对真实 Mosquitto 做 TLS 认证）→ 媒体登记（generic）→
        媒体凭证（--from-enrollment）→ media start → 云端 SRT 发布可用（MediaMTX ready）
  001   firebot-vehicle-01 增量：G1 readonly agent 运行中；fleet register 被拒（不重新 enroll、
        不生成/轮换 MQTT）；60-onboard --media-only bundle → media install/credentials/start；
        001 专项登记入口；G1 状态全程不变
负测：重复接入（幂等、旧 device token 有效）、错误身份 bundle、错误 Transport、错误凭证（云端拒绝
→ AUTH_REJECTED 退避，不刷）、安装中断、注册途中失败（token 不消费）、回滚；双路独立路径与相机
缺失；局域网与云同时取流；断网恢复；主密钥/车辆凭证 argv 与日志扫描。

真实组件：隔离 PostgreSQL/Redis、uvicorn API（enrollment + media authorize）、Mosquitto 2.0（TLS、
password_file、ACL；60-onboard 真实 mosquitto_passwd + SIGHUP）、MediaMTX 1.18.2、Ubuntu 20.04 用户态
（ld 2.31 / python3.8 / FFmpeg 4.2.7）+ build-libsrt.sh 产出的 libsrt 1.4.4。
视频源：web_video_server 兼容 MJPEG 测试图案 —— SYNTHETIC，**不作为真实相机硬件验收证据**。
替身（只替换宿主管理面）：docker / systemctl / curl(https→隔离 API) / sudo(chown)，见 shims/。

用法：python3 run_acceptance.py <phase>   phase = new | adopt001 | negatives
输出：EVIDENCE <KEY>=<VALUE> 行 + 末行 ACCEPTANCE_<PHASE>=PASS|FAIL。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rig  # noqa: E402

REPO = rig.REPO
OUT = Path("/sessions/confident-wonderful-pasteur/mnt/outputs")
FOCALPY = str(OUT / "env/focalpy.sh")
LIBSRT_DIR = OUT / "env/libsrt-from-script"
SHIMS = HERE / "shims"
FAIL: list[str] = []
EVID: dict = {}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)[:500]), flush=True)
    if not cond:
        FAIL.append(name)


def evidence(key, value):
    EVID[key] = value
    print(f"EVIDENCE {key}={value}", flush=True)


class Env:
    """隔离 runtime（服务器侧）+ broker + API + MediaMTX + MJPEG 源 + 替身 PATH。"""

    def __init__(self):
        self.w = rig.tempdir()
        self.st = rig.Stack(self.w)
        self.runtime = self.w / "runtime"
        for d in ("env", "secrets/app", "secrets/mosquitto/certs", "secrets/vehicles"):
            (self.runtime / d).mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime / "secrets", 0o700)
        (self.runtime / "secrets/app/media_publish_token").write_text(rig.MASTER + "\n")
        (self.runtime / "secrets/app/srt_publish_passphrase").write_text(rig.PASSPHRASE + "\n")
        (self.runtime / "secrets/app/mqtt_platform_password").write_text("platform-iso-pw\n")
        for f in (self.runtime / "secrets/app").iterdir():
            f.chmod(0o600)
        self.log = self.w / "shim.log"
        self.log.touch()
        self.sd = self.w / "sd"
        self.sd.mkdir()
        self.bin = self.w / "bin"
        self.bin.mkdir()
        for name in ("docker", "systemctl", "curl", "sudo"):
            (self.bin / name).write_text((SHIMS / name).read_text())
            (self.bin / name).chmod(0o755)
        self.repo = self._release_repo()

    def _release_repo(self) -> Path:
        """精确 SHA 交付源：当前仓库 HEAD 的干净检出（install 要求 clean + HEAD==SHA）。"""
        dst = self.w / "ROBOT-Web"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--depth", "1", f"file://{REPO}", str(dst)],
                       check=True, capture_output=True)
        self.sha = subprocess.run(["git", "-C", str(dst), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        return dst

    # ---------------- broker (real mosquitto, onboarding writes its password_file) ----------
    def start_broker(self):
        certs = self.runtime / "secrets/mosquitto/certs"
        subprocess.run(["bash", str(REPO / "scripts/generate_readonly_test_certs.sh"), str(certs)],
                       check=True, capture_output=True)
        mos = self.runtime / "secrets/mosquitto"
        (mos / "passwords").touch()
        subprocess.run(["mosquitto_passwd", str(mos / "passwords"), "platform"],
                       input="platform-iso-pw\nplatform-iso-pw\n", text=True, check=True, capture_output=True)
        acl = (REPO / "infra/mosquitto/acl.example").read_text()
        (mos / "acl").write_text(acl.replace("pattern write robot/%u/command_ack",
                                             "pattern write robot/%u/command_ack"))
        self.broker_dir = self.w / "broker"
        self.broker_dir.mkdir()
        for n in ("passwords", "acl"):
            shutil.copy(mos / n, self.broker_dir / n)
        self.broker_port = rig.free_port()
        conf = self.broker_dir / "mosquitto.conf"
        conf.write_text("\n".join([
            f"listener {self.broker_port} 127.0.0.1", "allow_anonymous false",
            f"password_file {self.broker_dir / 'passwords'}", f"acl_file {self.broker_dir / 'acl'}",
            f"cafile {certs / 'ca.crt'}", f"certfile {certs / 'server.crt'}",
            f"keyfile {certs / 'server.key'}", "persistence false", "log_dest stdout"]) + "\n")
        self.broker = self.st.spawn("mosquitto", ["mosquitto", "-c", str(conf)])
        (self.w / "broker.pid").write_text(str(self.broker.pid))
        assert rig.wait_port(self.broker_port, 15), "broker"
        (self.runtime / "env/public-activation.env").write_text(
            "MQTT_PUBLIC_HOSTNAME=firebot.youdaozd.com\nPUBLIC_DOMAIN=firebot.youdaozd.com\n"
            "MEDIA_SRT_PUBLIC_HOSTNAME=media-srt.isolated.test\nMEDIA_SRT_PUBLIC_PORT=8890\n")

    def start_cloud(self):
        self.st.start_api_mtx()
        self.wvs = rig.FakeWVS(rig.free_port(), {
            "/camera/color/image_raw": "testsrc=size=640x480:rate=15",
            "/thermal/image": "testsrc2=size=640x480:rate=15",
        })

    def env(self, **extra):
        e = dict(os.environ)
        e.update(
            PATH=f"{self.bin}:{os.environ['PATH']}", SHIM_LOG=str(self.log), SHIM_REPO=str(REPO),
            SHIM_PY=sys.executable, SHIM_BROKER_PID_FILE=str(self.w / "broker.pid"),
            SHIM_BROKER_DIR=str(self.broker_dir), SHIM_BROKER_PORT=str(self.broker_port),
            SHIM_API_PORT=str(self.st.api_port), U_DAO_RUNTIME_ROOT=str(self.runtime),
            U_DAO_REPO_ROOT=str(self.repo), SERVER_ENV_FILE=str(self.w / "none.env"),
            SD_STATE=str(self.sd), SD_PY=sys.executable, SD_MQTT_PROBE=str(SHIMS / "mqtt_probe.py"),
            SD_BROKER_PORT=str(self.broker_port),
            SD_BROKER_CA=str(self.runtime / "secrets/mosquitto/certs/ca.crt"),
            SD_MEDIA_PYTHON=FOCALPY, FIREBOT_TEST_SYSTEMCTL=str(self.bin / "systemctl"),
            FIREBOT_TEST_CURL=str(self.bin / "curl"), FIREBOT_TEST_SKIP_ROOT="1",
            FIREBOT_TEST_SKIP_CHOWN="1", FIREBOT_TEST_SYSTEMD_DIR=str(self.w / "units"),
            FIREBOT_TEST_WRAP_DIR=str(self.w / "wrap"), FIREBOT_MEDIA_PYTHON=FOCALPY,
            FIREBOT_MEDIA_TEST_PYTHON=FOCALPY, MEDIA_PUBLISH_TOKEN=rig.MASTER,
            ACL_CHECK_IMAGE="isolated", FIREBOT_READONLY_MQTT_WAIT_SECONDS="10",
        )
        (self.w / "units").mkdir(exist_ok=True)
        (self.w / "wrap").mkdir(exist_ok=True)
        e.update(extra)
        return e

    def sh(self, args, stdin=None, timeout=120, **extra):
        r = subprocess.run(args, input=stdin, capture_output=True, text=True, env=self.env(**extra),
                           timeout=timeout, cwd=str(self.repo))
        return r

    def ctl(self, *args, edge=None, stdin=None, timeout=120, **extra):
        if edge:
            extra["FIREBOT_EDGE_HOME"] = str(edge)
        return self.sh(["bash", str(self.repo / "integration/ros1/firebotctl"), *args], stdin=stdin,
                       timeout=timeout, **extra)

    def api_py(self, code, stdin=""):
        env = dict(os.environ, PYTHONPATH=str(REPO / "apps/api"))
        return subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, text=True,
                              env=env, cwd=str(REPO / "apps/api"), timeout=60)

    def edge_home(self, name, runtime_user="tl") -> Path:
        home = self.w / name
        r = self.sh(["bash", str(self.repo / "integration/ros1/vehicle-edge/lifecycle/edge-init.sh"),
                     "--runtime-user", runtime_user, "--edge-home", str(home)])
        assert r.returncode == 0, r.stdout + r.stderr
        shutil.copy(self.runtime / "secrets/mosquitto/certs/ca.crt", home / "certs/mqtt-ca.crt")
        (home / "config/bootstrap.env").write_text("FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n")
        return home

    def media_profile_override(self, home: Path):
        """把 Hardware Profile 中的 web_video_server 端口指向隔离 MJPEG 源（现场为 8080）。"""
        p = home / "config/media-hardware.env"
        p.write_text(p.read_text().replace("127.0.0.1:8080", f"127.0.0.1:{self.wvs.port}"))
        m = home / "config/media.env"
        extra = (f"FIREBOT_MEDIA_SRT_HOST=127.0.0.1\nFIREBOT_MEDIA_SRT_PORT={self.st.srt_port}\n"
                 "FIREBOT_MEDIA_ENCODER=libx264\nFIREBOT_MEDIA_BACKOFF_INITIAL_S=1\n"
                 "FIREBOT_MEDIA_BACKOFF_MAX_S=4\nFIREBOT_MEDIA_AUTH_BACKOFF_S=5\n")
        txt = m.read_text()
        for k in ("FIREBOT_MEDIA_SRT_HOST", "FIREBOT_MEDIA_SRT_PORT"):
            txt = "\n".join(ln for ln in txt.splitlines() if not ln.startswith(k + "="))
        m.write_text(txt + "\n" + extra)

    def close(self):
        for f in self.sd.glob("firebot-media@*.pid"):
            try:
                os.killpg(int(f.read_text()), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        for f in self.sd.glob("firebot-vehicle-edge.pid"):
            try:
                os.killpg(int(f.read_text()), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.wvs.close()
        except Exception:  # noqa: BLE001
            pass
        self.st.close()


def db_cleanup(vehicle_ids):
    from app.db.models import AuditLog, Base, EnrollmentToken, Robot, StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    with SessionLocal() as db:
        for vid in vehicle_ids:
            r = db.scalar(select(Robot).where(Robot.vehicle_id == vid))
            db.execute(delete(EnrollmentToken).where(EnrollmentToken.device_id == vid))
            if not r:
                continue
            db.execute(delete(StreamRegistry).where(StreamRegistry.robot_id == r.id))
            db.execute(delete(AuditLog).where(AuditLog.robot_id == r.id))
            for t in reversed(Base.metadata.sorted_tables):
                if "robot_id" in t.c and t.name != "robots":
                    db.execute(t.delete().where(t.c.robot_id == r.id))
            db.execute(delete(Robot).where(Robot.id == r.id))
        db.commit()


def scan_secrets(env: Env, secrets: list[str], label: str):
    """argv（shim 日志 + 存活进程 cmdline）与所有日志文件中不得出现任何 secret。"""
    hits = []
    texts = [("shim.log", env.log.read_text(errors="replace"))]
    for f in list(env.w.glob("*.log")) + list(env.sd.glob("*.log")):
        texts.append((f.name, f.read_text(errors="replace")))
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                texts.append((f"proc{pid}", open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")))
            except OSError:
                pass
    for name, txt in texts:
        for s in secrets:
            if s and len(s) >= 8 and s in txt:
                hits.append(f"{name}:{s[:6]}…")
    check(f"SECRET_SCAN[{label}] argv/日志零泄露（{len(texts)} 源 × {len(secrets)} secret）", not hits, hits[:5])
    return not hits


# ============================================================== NEW vehicle (firebot-vehicle-03)
def phase_new(env: Env):
    vid = "firebot-vehicle-03"
    db_cleanup([vid])
    # 1) 预登记（正式入口：firebotctl fleet preregister）
    r = env.ctl("fleet", "preregister", vid, "--name", "firebot-003", "--site-code", "DEMO_PARKING")
    check("NEW 预登记 PREREGISTER", r.returncode == 0 and "PREREGISTER=" in r.stdout, r.stdout + r.stderr)
    # 2) 凭证准备：未给 Profile → REJECTED（零凭证）
    r = env.ctl("fleet", "register", vid)
    check("NEW fleet register 无 Profile → REJECTED（无默认 8883）",
          r.returncode != 0 and "PROFILE_REQUIRED" in r.stderr
          and not (env.runtime / f"secrets/vehicles/{vid}").exists(), r.stderr[-300:])
    r = env.ctl("fleet", "register", vid, "--transport-profile", "u-dao-standard-v1",
                "--media-srt-port", str(env.st.srt_port))
    check("NEW fleet register PASS（批准 Profile → 真实 Mosquitto 凭证 + 媒体凭证 + token 文件）",
          r.returncode == 0 and "FLEET_REGISTER=PASS" in r.stdout, r.stdout[-600:] + r.stderr[-600:])
    vdir = env.runtime / f"secrets/vehicles/{vid}"
    token_file = vdir / "enroll-token"
    check("NEW broker 零重启（SIGHUP reload）", "MOSQUITTO_ZERO_RESTART=PASS" in r.stdout)
    check("NEW live ACL verify（真实 broker 行为级）", "VEHICLE_ACL_LIVE=PASS" in r.stdout, r.stdout[-400:])
    check("NEW ENROLL_TOKEN 不回显", token_file.read_text().strip() not in r.stdout + r.stderr)
    mqtt_pw = (vdir / "mqtt_password").read_text().strip()
    srt_tok = (vdir / "srt_publish_token").read_text().strip()
    # 3) 重复接入（服务器重复 register）：凭证不旋转；车上未来 device token 在 activate 前不变
    r2 = env.ctl("fleet", "register", vid, "--transport-profile", "u-dao-standard-v1",
                 "--media-srt-port", str(env.st.srt_port))
    check("NEW 重复 register 幂等（MQTT 凭证不旋转）",
          r2.returncode == 0 and f"{vid}_EXISTING_CREDENTIAL_REUSED=PASS" in r2.stdout
          and (vdir / "mqtt_password").read_text().strip() == mqtt_pw, r2.stdout[-300:])
    # 4) 车端：edge-init → enroll（token 文件；正式 enrollment API）
    edge = env.edge_home("edge-new")
    stage = env.w / "token-for-vehicle"
    shutil.copy(token_file, stage)
    stage.chmod(0o600)
    r = env.ctl("vehicle", "enroll", vid, "--token", stage.read_text().strip(), edge=edge)
    check("NEW enroll --token <值> 被拒（argv）", r.returncode != 0 and "TOKEN_ON_ARGV_FORBIDDEN" in r.stderr)
    r = env.ctl("vehicle", "enroll", vid, "--token-file", str(stage), edge=edge,
                FIREBOT_EDGE_HOME=str(edge))
    check("NEW enroll PASS（Transport Profile + 媒体凭证下发）",
          r.returncode == 0 and "ENROLL=PASS" in r.stdout and "MEDIA_CREDENTIALS=RECEIVED" in r.stdout,
          r.stdout[-500:] + r.stderr[-500:])
    a = (edge / "config/assignment.env").read_text()
    check("NEW assignment 来自 Profile（u-dao-standard-v1/8883/canonical-mqtt）",
          "TRANSPORT_PROFILE=u-dao-standard-v1" in a and "MQTT_PORT=8883" in a, a)
    r = env.ctl("vehicle", "enroll", vid, "--token-file", str(stage), edge=edge)
    check("NEW token 重放被拒", r.returncode != 0)
    # 5) 精确 SHA 安装：edge-install + media install（安装 ≠ 启动）
    r = env.sh(["bash", str(env.repo / "integration/ros1/vehicle-edge/lifecycle/edge-install.sh"),
                str(env.repo)], FIREBOT_EDGE_HOME=str(edge), FIREBOT_REQUIRE_SHA=env.sha)
    check("NEW edge-install 精确 SHA", r.returncode == 0 and "EDGE_INSTALL=PASS" in r.stdout,
          r.stdout[-400:] + r.stderr[-300:])
    r = env.ctl("vehicle", "media", "install", "--sha", env.sha, "--libsrt-prebuilt", str(LIBSRT_DIR),
                edge=edge)
    check("NEW media install 精确 SHA（不启动）", r.returncode == 0 and "MEDIA_SERVICE_STATE=inactive" in r.stdout,
          r.stdout[-400:] + r.stderr[-400:])
    # 6) 只读启动（readonly-start → 真实 MQTT-TLS 认证）
    r = env.sh(["bash", str(env.repo / "integration/ros1/vehicle-edge/lifecycle/readonly-start.sh")],
               FIREBOT_EDGE_HOME=str(edge))
    check("NEW readonly-start PASS（下发凭证对真实 Mosquitto TLS 认证）",
          r.returncode == 0 and "READONLY_BRIDGE_START=PASS" in r.stdout, r.stdout[-600:])
    # 7) 媒体登记（generic，显式目标 + 批准 Profile）；未登记前云端拒绝发布
    reg = [sys.executable, "-m", "app.dev.register_vehicle_streams", "generic", "--vehicle-id", vid,
           "--transport-profile", "u-dao-standard-v1"]
    rr = subprocess.run(reg + ["--apply"], capture_output=True, text=True, cwd=str(REPO / "apps/api"),
                        env=dict(os.environ, PYTHONPATH=str(REPO / "apps/api")))
    check("NEW 媒体登记 generic --apply", rr.returncode == 0, rr.stderr[-300:])
    (env.w / "receipt-new.json").write_text(rr.stdout)
    # 8) 媒体凭证导入（来自 enrollment）→ preflight → start
    r = env.ctl("vehicle", "media", "credentials", "import", "--from-enrollment", edge=edge)
    check("NEW media credentials --from-enrollment", r.returncode == 0 and "IMPORTED" in r.stdout,
          r.stdout + r.stderr)
    env.media_profile_override(edge)
    r = env.ctl("vehicle", "media", "start", edge=edge)
    check("NEW media start", r.returncode == 0 and "MEDIA_START=PASS" in r.stdout, r.stdout[-400:] + r.stderr[-400:])
    s1 = rig.wait_state(edge, "roof_rgb", {"LIVE"}, 40).get("state")
    s2 = rig.wait_state(edge, "roof_thermal", {"LIVE"}, 40).get("state")
    p1 = rig.wait_ready(env.st, f"{vid}-roof_rgb", 20)
    p2 = rig.wait_ready(env.st, f"{vid}-roof_thermal", 20)
    check("NEW 双路 LIVE + MediaMTX ready（独立路径）", s1 == s2 == "LIVE" and bool(p1 and p1.get("ready"))
          and bool(p2 and p2.get("ready")), (s1, s2, p1, p2))
    evidence("NEW_STREAMS", f"{vid}-roof_rgb,{vid}-roof_thermal ready={bool(p1 and p1.get('ready'))},{bool(p2 and p2.get('ready'))}")
    # 9) 局域网与云同时取流（web_video_server 多消费者；发布器不独占）
    n = 0
    with urllib.request.urlopen(f"http://127.0.0.1:{env.wvs.port}/stream?topic=/camera/color/image_raw&type=mjpeg",
                                timeout=5) as resp:
        end = time.time() + 3
        while time.time() < end:
            if b"image/jpeg" in resp.readline():
                n += 1
    check("NEW 局域网与云同时取流", n >= 20 and env.st.paths()[f"{vid}-roof_rgb"]["ready"], n)
    evidence("LOCAL_CLOUD_COEXIST", f"lan_frames_3s={n} cloud_ready=True clients={dict(env.wvs.clients)}")
    # 10) 相机缺失（thermal 源停）→ thermal SOURCE_STALLED，RGB 不受影响 → 恢复
    env.wvs.paused.add("/thermal/image")
    st = rig.wait_state(edge, "roof_thermal", {"SOURCE_STALLED", "SOURCE_UNAVAILABLE"}, 25)
    check("NEW 相机缺失：thermal 降级、RGB 仍 LIVE", st.get("state") in ("SOURCE_STALLED", "SOURCE_UNAVAILABLE")
          and rig.read_state(edge, "roof_rgb").get("state") == "LIVE", st)
    env.wvs.paused.discard("/thermal/image")
    check("NEW 相机恢复后自动续推", rig.wait_state(edge, "roof_thermal", {"LIVE"}, 40).get("state") == "LIVE")
    # 11) 长时间断网（MediaMTX 下线 45s）→ 恢复后自愈（进程不重启）
    pids = (rig.read_state(edge, "roof_rgb").get("pid"), rig.read_state(edge, "roof_thermal").get("pid"))
    env.st.mtx.kill()
    env.st.mtx.wait()
    down_at = time.time()
    check("NEW 断网 → NETWORK_DOWN", rig.wait_state(edge, "roof_rgb", {"NETWORK_DOWN"}, 20).get("state") == "NETWORK_DOWN")
    time.sleep(max(0, 45 - (time.time() - down_at)))
    env.st.mtx = env.st.spawn("mediamtx-2", [rig.MEDIAMTX, str(env.w / "mediamtx.yml")])
    rig.wait_port(env.st.api_mtx, 20)
    a1 = rig.wait_state(edge, "roof_rgb", {"LIVE"}, 40)
    a2 = rig.wait_state(edge, "roof_thermal", {"LIVE"}, 40)
    check("NEW 断网 45s 后自愈（同进程、无 StartLimit）", a1.get("state") == a2.get("state") == "LIVE"
          and (a1.get("pid"), a2.get("pid")) == pids, (a1, a2, pids))
    evidence("NETWORK_OUTAGE_RECOVERY", f"outage=45s recovered_after={time.time() - down_at:.0f}s same_pid=True reconnects={a1.get('reconnects')}")
    # 12) 撤销登记前发布会话门：活动 → 拒绝
    rr = subprocess.run(reg + ["--rollback", "-", "--apply"], input=(env.w / "receipt-new.json").read_text(),
                        capture_output=True, text=True, cwd=str(REPO / "apps/api"),
                        env=dict(os.environ, PYTHONPATH=str(REPO / "apps/api"),
                                 FIREBOT_MEDIAMTX_API=f"http://127.0.0.1:{env.st.api_mtx}"))
    check("NEW 撤销登记：活动发布会话 → 拒绝（零删除）", rr.returncode == 5 and "活动发布会话" in rr.stderr, rr.stderr[-300:])
    # 13) stop → 撤销 → 云端发布被拒（unregistered）
    r = env.ctl("vehicle", "media", "stop", edge=edge)
    time.sleep(2)
    rr = subprocess.run(reg + ["--rollback", "-", "--apply"], input=(env.w / "receipt-new.json").read_text(),
                        capture_output=True, text=True, cwd=str(REPO / "apps/api"),
                        env=dict(os.environ, PYTHONPATH=str(REPO / "apps/api"),
                                 FIREBOT_MEDIAMTX_API=f"http://127.0.0.1:{env.st.api_mtx}"))
    check("NEW stop 后撤销登记 PASS（MediaMTX 确认无发布）", rr.returncode == 0 and r.returncode == 0, rr.stderr[-300:])
    scan_secrets(env, [rig.MASTER, rig.PASSPHRASE, mqtt_pw, srt_tok, stage.read_text().strip()], "NEW")
    db_cleanup([vid])


def main():
    phase = sys.argv[1]
    env = Env()
    try:
        env.start_broker()
        env.start_cloud()
        {"new": phase_new, "adopt001": globals().get("phase_adopt001"),
         "negatives": globals().get("phase_negatives")}[phase](env)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check(f"PHASE_{phase} 未预期异常 {type(exc).__name__}", False, exc)
    finally:
        env.close()
    (OUT / "env" / f"acceptance-{phase}.json").write_text(json.dumps({"fail": FAIL, "evidence": EVID}, ensure_ascii=False, indent=1))
    print(f"ACCEPTANCE_{phase.upper()}={'PASS' if not FAIL else 'FAIL'} fails={len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
