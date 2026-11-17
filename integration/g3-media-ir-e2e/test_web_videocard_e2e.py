"""G3 正式浏览器验收：生产构建 Firebot Web 的 VideoCard（非独立 WHEP 测试页）。

拓扑（全部 127.0.0.1 隔离端口；与 infra/nginx/nginx.server.conf 路由语义一致）：
  Chromium → web_gateway.mjs ─┬─ / → apps/web 生产构建 dist（WEB_DIST）
                              ├─ /api/, /ws/ → uvicorn app.main（真实 API，隔离 PG/Redis）
                              └─ /media/ → MediaMTX WHEP（authHTTP → 真实 API 鉴权）
  media-test-source（SIMULATED 叠字）经 SRT 发布：firebot-sim-ir-01-roof_rgb / -roof_thermal，
  使用 SYNTHETIC_TEST 登记 + firebot-sim-* 白名单 + 逐流 HMAC 口令（主密钥只给文件路径）。
  ir_telemetry_sim（固定身份 firebot-sim-ir-01）经 MQTT→Adapter→Ingress→PG/Redis 上报温度。

验证：两路 VideoCard 真实解码且帧变化；断流后不显示 LIVE、恢复后重连解码；
温度 27.5/42.0/67.8 在 PG SensorSample、Redis latest、API snapshot、Web KPI 一致；
thermal=null → Web“未知”；模拟横幅；真实 001 无任何传感器写入。
需要 REAL_E2E=1、G3_MEDIAMTX、ffmpeg、CHROMIUM_PATH、WEB_DIST（vite build 产物）；否则 SKIP。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import (
    REPO_ROOT,
    SIM,
    free_port,
    mediamtx_binary,
    run_sim,
    spawn,
    stop,
    wait_for,
    wait_port,
)

MASTER = "g3-web-media-master-isolated-0123456789ab"
PASSPHRASE = "g3-web-srt-passphrase-isolated"
RGB = f"{SIM}-roof_rgb"
THERMAL = f"{SIM}-roof_thermal"
REAL_VEHICLES = ("R001", "firebot-vehicle-01")
BROWSER = REPO_ROOT / "integration/g3-media-ir-e2e/browser"
EVIDENCE: dict = {}


def _evidence_dir() -> Path:
    path = Path(os.environ.get("G3_EVIDENCE_DIR") or (REPO_ROOT / ".g3-web-evidence"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _admin_credentials() -> tuple[str, str]:
    """G2 E2E 的隔离测试管理员（按文件加载，避免与本目录 conftest 同名冲突）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "g2_conftest_web", REPO_ROOT / "integration" / "g2-command-loop-e2e" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ensure_isolated_test_admin()


@pytest.fixture(scope="module")
def web(g3):
    dist = os.environ.get("WEB_DIST")
    chromium = os.environ.get("CHROMIUM_PATH")
    if not (mediamtx_binary() and shutil.which("ffmpeg") and chromium and dist):
        pytest.skip("MediaMTX/ffmpeg/CHROMIUM_PATH/WEB_DIST 未齐：生产 VideoCard 验收 NOT_RUN")
    if not (Path(dist) / "index.html").is_file():
        pytest.skip("WEB_DIST 不是 vite build 产物")
    workdir = g3["workdir"]
    os.environ["MEDIA_PUBLISH_TOKEN"] = MASTER
    os.environ["SYNTHETIC_MEDIA_VEHICLE_IDS"] = SIM
    from app.core.config import get_settings

    get_settings.cache_clear()
    _register_streams()
    real_before = _real_sensor_counts()
    api_port, srt, whep, ctl, rtsp, gw = (free_port() for _ in range(6))
    env = {
        **g3["base"],
        "MEDIA_PUBLISH_TOKEN": MASTER,
        "SYNTHETIC_MEDIA_VEHICLE_IDS": SIM,
        "ALLOWED_ORIGINS": f"http://127.0.0.1:{gw}",
    }
    master_file = workdir / "web-media-master"
    master_file.write_text(MASTER)
    master_file.chmod(0o600)
    api = spawn(
        workdir,
        "web-api",
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
    assert wait_port(api_port, 30), (workdir / "web-api.log").read_text()[-1500:]
    conf = workdir / "web-mediamtx.yml"
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
                "paths:",
                "  all_others:",
                f"    srtPublishPassphrase: {PASSPHRASE}",
            ]
        )
        + "\n"
    )
    mtx = spawn(workdir, "web-mediamtx", [mediamtx_binary(), str(conf)], dict(os.environ))
    assert wait_port(ctl, 15)
    gateway = spawn(
        workdir,
        "web-gateway",
        ["node", str(BROWSER / "web_gateway.mjs"), str(gw), dist, str(api_port), str(whep)],
        dict(os.environ),
    )
    assert wait_port(gw, 15)
    ctx = {
        "workdir": workdir,
        "base_url": f"http://127.0.0.1:{gw}",
        "srt": srt,
        "ctl": ctl,
        "master_file": master_file,
        "env": env,
        "publishers": {},
        "chromium": chromium,
        "real_before": real_before,
        "redis": g3["redis"],
        "api_port": api_port,
    }
    for stream_id, kind in ((RGB, "rgb"), (THERMAL, "thermal")):
        _publish(ctx, stream_id, kind)
    for stream_id in (RGB, THERMAL):
        assert wait_for(lambda s=stream_id: _ready(ctx, s), 30), stream_id
    # 模拟设备在线 + 初始温度（Web 监控页需要 ONLINE 才显示数值）
    run_sim(g3, "--values", "27.5", "--interval", "1.2")
    try:
        yield ctx
    finally:
        for proc in ctx["publishers"].values():
            stop(proc)
        stop(gateway)
        stop(mtx)
        stop(api)
        _unregister_streams()
        (_evidence_dir() / "g3-web-evidence.json").write_text(
            json.dumps(EVIDENCE, ensure_ascii=False, indent=2)
        )


def _register_streams():
    from app.db.models import Robot, StreamRegistry
    from app.db.session import SessionLocal
    from app.modules.media.router import SYNTHETIC_TEST_SOURCE
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
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
    from app.db.models import StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import delete

    with SessionLocal() as db:
        db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id.in_([RGB, THERMAL])))
        db.commit()


def _real_sensor_counts() -> dict:
    from app.db.models import Robot, SensorSample
    from app.db.session import SessionLocal
    from sqlalchemy import func, select

    counts = {}
    with SessionLocal() as db:
        for vid in REAL_VEHICLES:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == vid))
            counts[vid] = (
                None
                if robot is None
                else db.scalar(
                    select(func.count())
                    .select_from(SensorSample)
                    .where(SensorSample.robot_id == robot.id)
                )
            )
    return counts


def _sim_samples() -> list[float]:
    from app.db.models import Robot, SensorSample
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        rows = db.scalars(
            select(SensorSample)
            .where(SensorSample.robot_id == robot.id)
            .order_by(SensorSample.id.desc())
            .limit(20)
        ).all()
        return [r.top_ir_max for r in rows]


def _publish(ctx, stream_id, kind):
    env = {
        k: v
        for k, v in ctx["env"].items()
        if k not in {"MEDIA_PUBLISH_TOKEN", "MEDIA_PUBLISH_MASTER"}
    }
    env.update(
        {
            "MEDIA_STREAM_ID": stream_id,
            "OUTPUT_PROTOCOL": "srt",
            "SRT_USER": "",  # 合成流：逐流 HMAC 口令（进程内派生）
            "MEDIA_PUBLISH_MASTER_FILE": str(ctx["master_file"]),
            "SRT_PUBLISH_PASSPHRASE": PASSPHRASE,
            "MEDIA_SRT_ORIGIN": f"srt://127.0.0.1:{ctx['srt']}",
            "SIMULATED_LABEL": f"{kind.upper()} {stream_id}",
            "SIMULATED_KIND": kind,
            "HEALTH_PORT": str(free_port()),
            "HEALTH_HOST": "127.0.0.1",
        }
    )
    ctx["publishers"][stream_id] = spawn(
        ctx["workdir"], f"web-pub-{stream_id}", ["node", "services/media-test-source/main.mjs"], env
    )


def _ready(ctx, stream_id):
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{ctx['ctl']}/v3/paths/get/{stream_id}", timeout=3
        ) as r:
            return json.loads(r.read()).get("ready")
    except OSError:
        return None


def _api_top_ir(ctx):
    """API 侧：GET /api/v1/monitor/snapshot 中模拟设备的 top_ir（真实 uvicorn，经鉴权）。"""
    import urllib.request

    username, password = _admin_credentials()
    base = f"http://127.0.0.1:{ctx['api_port']}/api/v1"
    req = urllib.request.Request(
        f"{base}/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        token = json.loads(r.read())["access_token"]
    req = urllib.request.Request(
        f"{base}/monitor/snapshot", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        robots = json.loads(r.read())["robots"]
    return next(x for x in robots if x["vehicle_id"] == SIM).get("top_ir")


def _browser(ctx, phase: str, extra: dict | None = None, timeout: int = 120) -> dict:
    username, password = _admin_credentials()
    result = subprocess.run(
        [
            "node",
            str(BROWSER / "web_videocard.mjs"),
            ctx["base_url"],
            SIM,
            str(_evidence_dir()),
            phase,
        ],
        input=json.dumps({"username": username, "password": password, **(extra or {})}),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "CHROMIUM_PATH": ctx["chromium"]},
    )
    assert result.returncode == 0, result.stderr[-1500:]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data.get("ok"), data
    return data


def test_production_videocard_rgb_and_thermal_decode(web):
    data = _browser(web, "play")
    EVIDENCE["play"] = data
    assert data["banner"] and "SIMULATED" in data["banner"] and SIM in data["banner"]
    for key in ("rgb", "thermal"):
        card = data["cards"][key]
        assert card["decoded_changing"], (key, card)
        r, g, b = card["hashes"][-1]["top"]
        assert r > 150 and g < 90 and b < 90, (key, card["hashes"][-1])  # 画面内 SIMULATED 红色横幅
    # 走的是生产 VideoCard 的票据 + /media/ WHEP 路径（同源网关 = nginx 语义）
    assert any("POST /api/v1/media/tickets" in r for r in data["requests"])
    assert any(r.startswith("POST /media/") and r.endswith("/whep") for r in data["requests"])


def test_temperatures_consistent_pg_redis_api_web(web, g3):
    ack = web["workdir"] / "temps-ack"
    ack.mkdir(exist_ok=True)
    expected = ["27.5℃", "42℃", "67.8℃", "未知"]
    proc = subprocess.Popen(
        [
            "node",
            str(BROWSER / "web_videocard.mjs"),
            web["base_url"],
            SIM,
            str(_evidence_dir()),
            "temps",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CHROMIUM_PATH": web["chromium"]},
    )
    username, password = _admin_credentials()
    proc.stdin.write(
        json.dumps(
            {"username": username, "password": password, "expect": expected, "ack_dir": str(ack)}
        )
    )
    proc.stdin.close()
    redis = web["redis"]
    rows = []
    for index, raw in enumerate(["27.5", "42.0", "67.8", "null"], start=1):
        run_sim(g3, "--values", raw, "--interval", "1.2")
        value = None if raw == "null" else float(raw)
        latest = wait_for(
            lambda v=value: (d := json.loads(redis.get(f"robot:{SIM}:latest") or "{}"))
            and d.get("top_ir", "x") == v
            and d,
            15,
        )
        assert latest, raw
        if value is not None:
            assert wait_for(lambda v=value: v in _sim_samples(), 10), (raw, _sim_samples())
        # 等浏览器读到本值后再推进下一个（Web 侧顺序断言）
        wait_for(lambda i=index: (ack / f"seen-{i}").exists(), 30)
        api_value = wait_for(lambda v=value: (x := _api_top_ir(web)) == v and ("ok", x), 10)
        assert api_value, (raw, _api_top_ir(web))
        rows.append(
            {
                "sent": raw,
                "redis_top_ir": latest.get("top_ir"),
                "api_top_ir": api_value[1],
                "pg_sample": value in _sim_samples() if value is not None else "n/a",
            }
        )
    proc.wait(timeout=60)
    out, err = proc.stdout.read(), proc.stderr.read()
    data = json.loads(out.strip().splitlines()[-1])
    assert data.get("ok"), (data, err[-800:])
    for row, web_row in zip(rows, data["temps"], strict=True):
        row["web"] = web_row["value"]
        row["web_label"] = web_row["label"]
        assert web_row["ok"], (row, web_row)
        assert web_row["label"] == "温度（模拟）"
    EVIDENCE["temps"] = rows


def test_stream_outage_and_reconnect_in_videocard(web):
    ready_file = web["workdir"] / "rc-ready"
    restored_file = web["workdir"] / "rc-restored"
    for f in (ready_file, restored_file):
        f.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            "node",
            str(BROWSER / "web_videocard.mjs"),
            web["base_url"],
            SIM,
            str(_evidence_dir()),
            "reconnect",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CHROMIUM_PATH": web["chromium"]},
    )
    username, password = _admin_credentials()
    proc.stdin.write(
        json.dumps(
            {
                "username": username,
                "password": password,
                "ready_file": str(ready_file),
                "restored_file": str(restored_file),
            }
        )
    )
    proc.stdin.close()
    assert wait_for(ready_file.exists, 60), "browser never connected"
    stop(web["publishers"].pop(RGB))  # 断流
    assert wait_for(lambda: not _ready(web, RGB), 20)
    time.sleep(12)  # 现场断流量级（>ICE 断开检测窗口）
    _publish(web, RGB, "rgb")  # 恢复
    assert wait_for(lambda: _ready(web, RGB), 30)
    restored_file.write_text("restored")
    proc.wait(timeout=90)
    out, err = proc.stdout.read(), proc.stderr.read()
    data = json.loads(out.strip().splitlines()[-1])
    assert data.get("ok"), (data, err[-800:])
    EVIDENCE["reconnect"] = data
    assert data["state_during_outage"], data  # 断流期间 VideoCard 离开 LIVE（不伪装在线）
    assert data["cards"]["rgb"]["decoded_after"], data


def test_simulation_never_writes_real_001_sensor_facts(web):
    # 浏览器验收全程（含 4 次模拟温度上报）后，真实车辆 SensorSample 计数不变，
    # Redis 中真实车辆 latest 不含任何模拟温度值
    assert _real_sensor_counts() == web["real_before"]
    for vid in REAL_VEHICLES:
        raw = web["redis"].get(f"robot:{vid}:latest")
        if raw:
            assert json.loads(raw).get("top_ir") not in (27.5, 42.0, 67.8), vid
    EVIDENCE["real_isolation"] = {"real_sensor_counts": web["real_before"], "unchanged": True}
