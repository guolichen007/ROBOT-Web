"""G3 正式浏览器验收：生产构建 Firebot Web 的 VideoCard（非独立 WHEP 测试页）。

拓扑（全部 127.0.0.1 隔离端口；与 infra/nginx/nginx.server.conf 路由语义一致）：
  Chromium → web_gateway.mjs ─┬─ / → apps/web 生产构建 dist（WEB_DIST）
                              ├─ /api/, /ws/ → uvicorn app.main（真实 API，隔离 PG/Redis）
                              └─ /media/ → MediaMTX WHEP（authHTTP → 真实 API 鉴权）
  media-test-source（SIMULATED 叠字）经 SRT 发布：firebot-sim-ir-01-roof_rgb / -roof_thermal，
  使用 SYNTHETIC_TEST 登记 + firebot-sim-* 白名单 + 逐流 HMAC 口令（主密钥只给文件路径）。
  模拟设备在线：sim_presence（固定身份 firebot-sim-ir-01）只上报 availability/status，
  **零 telemetry**。

G3-R2 产品契约：无独立红外测温传感器——热成像视频保留，红外温度数值停用。
验证：两路 VideoCard（RGB + thermal）真实解码且帧变化；首帧门控；双路 60s；自动/手动恢复；
切车隔离；模拟横幅（SIMULATED）；Web 首页不显示温度卡片——无温度数据时不显示，
即使云端收到历史/兼容温度数据（显式兼容开关注入）也不显示；真实 001 无任何传感器写入。
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
    CREATED,
    REPO_ROOT,
    SIM,
    SIM_PASSWORD,
    _assert_sim_namespace,
    free_port,
    mediamtx_binary,
    redact_text,
    run_presence,
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
def web(g3, real_001_asset):
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
    # G3-R2：模拟设备在线只上报 availability/status（不发送任何 telemetry / 温度）
    sensor_before = len(_sim_samples())
    run_presence(g3, "--hold-seconds", "1")
    ctx["sim_samples_after_presence"] = (sensor_before, len(_sim_samples()))
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

    _assert_sim_namespace(SIM)
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        for stream_id, camera in ((RGB, "roof_rgb"), (THERMAL, "roof_thermal")):
            if not db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id)):
                CREATED["streams"].add(stream_id)
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
        mine = [sid for sid in (RGB, THERMAL) if sid in CREATED["streams"]]
        if mine:
            db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id.in_(mine)))
            CREATED["streams"].difference_update(mine)
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


def _redact(text: str) -> str:
    """G3-R2 SECRET_LOG_REDACTION：失败断言/日志尾部输出前掩码全部测试凭据。"""
    return redact_text(text, (_admin_credentials()[1], SIM_PASSWORD, MASTER, PASSPHRASE))


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
    assert result.returncode == 0, _redact(result.stderr[-1500:])
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data.get("ok"), _redact(json.dumps(data, ensure_ascii=False)[-1500:])
    return data


def test_production_videocard_rgb_and_thermal_decode(web):
    data = _browser(web, "play")
    EVIDENCE["play"] = data
    assert data["banner"] and "SIMULATED" in data["banner"] and SIM in data["banner"]
    for key in ("rgb", "thermal"):
        card = data["cards"][key]
        assert card["decoded_changing"], (key, card)
        gate = card["gate"]
        # 首帧门控：「实时」只在 data-first-frame=yes 后出现，且先经历“连接中/等待首帧”
        assert gate["violation"] is False and gate["live_at_ms"] is not None, gate
        assert gate["connecting_seen"], gate
        r, g, b = card["hashes"][-1]["top"]
        assert r > 150 and g < 90 and b < 90, (key, card["hashes"][-1])  # 画面内 SIMULATED 红色横幅
    # 走的是生产 VideoCard 的票据 + /media/ WHEP 路径（同源网关 = nginx 语义）
    assert any("POST /api/v1/media/tickets" in r for r in data["requests"])
    assert any(r.startswith("POST /media/") and r.endswith("/whep") for r in data["requests"])
    # G3-R2：双路视频播放中首页仍固定四项指标、无温度卡片；热成像视频本身保留
    assert data["temperature_cards"] == 0 and data["kpi_text_has_temp"] is False, data
    assert len(data["kpi_labels"]) == 4, data


def test_video_full_fov_contain_and_fullscreen(web):
    """视野保护：RGB / thermal 生产 VideoCard 完整视野等比例（object-fit: contain，零裁切），
    卡片内与全屏一致；1920×1080 / 1366×768 / 390×844 不裁切、无横向溢出；退出全屏后仍实时。"""
    data = _browser(web, "fov", timeout=150)
    EVIDENCE["fov"] = {"cards": data["fov"], "viewports": data["fov_viewports"]}
    for key in ("rgb", "thermal"):
        for mode in ("card", "fullscreen", "after_exit"):
            g = data["fov"][key][mode]
            assert g["ok"] and g["fit"] == "contain", (key, mode, g)
            assert g["full_fov"] is True and g["aspect_error"] < 0.01, (key, mode, g)
        assert data["fov"][key]["fullscreen"]["in_fullscreen"] is True, key
        assert "video-stage" in (data["fov"][key]["fullscreen"]["element"] or ""), key
        assert data["fov"][key]["live_after_exit"] is True, key
    for vp in data["fov_viewports"]:
        width = int(vp["viewport"].split("x")[0])
        assert vp["scroll_w"] <= width + 1, vp
        for key in ("rgb", "thermal"):
            g = vp[key]
            assert g["full_fov"] is True and g["aspect_error"] < 0.01, (vp["viewport"], key)


def test_no_temperature_card_without_sensor_and_with_legacy_data(web, g3):
    """G3-R2 TEMPERATURE_UI_DISABLED（真实生产构建 Web + 真实 API/Ingress）。

    1) 产品路径：模拟设备只上报在线状态（零 telemetry）→ PG 无新增 SensorSample、Web 无温度卡片；
    2) 历史 / 兼容数据：显式兼容开关注入 thermal.max_temp=67.8 → 云端照常保存（兼容），
       但 Web 首页仍固定四项指标、无温度卡片、页面无“温度 / ℃ / 67.8”。
    """
    before, after = web["sim_samples_after_presence"]
    assert after == before, "presence must not create SensorSample"
    data = _browser(web, "kpis")
    assert data["kpi_labels"] == ["智能车状态", "电池", "烟雾", "通信状态 / 接收延迟"], data
    assert data["temperature_cards"] == 0 and data["kpi_text_has_temp"] is False, data
    # 历史 / 兼容温度数据到达云端（兼容链路保留）
    run_sim(g3, "--values", "67.8", "--interval", "1.2")
    redis = web["redis"]
    assert wait_for(
        lambda: json.loads(redis.get(f"robot:{SIM}:latest") or "{}").get("top_ir") == 67.8, 15
    )
    assert wait_for(lambda: 67.8 in _sim_samples(), 10)
    assert _api_top_ir(web) == 67.8  # API 字段保留（不破坏协议）
    legacy = _browser(web, "kpis", {"forbid_text": "67.8"})
    assert legacy["kpi_labels"] == data["kpi_labels"], legacy
    assert legacy["temperature_cards"] == 0 and legacy["kpi_text_has_temp"] is False, legacy
    assert legacy["page_has_forbidden"] is False, legacy
    EVIDENCE["temperature_ui_disabled"] = {
        "presence_sensor_samples_delta": after - before,
        "no_sensor": {k: data[k] for k in ("kpi_labels", "temperature_cards", "kpi_text_has_temp")},
        "legacy_67_8_in_api": True,
        "legacy_web": {
            k: legacy[k]
            for k in ("kpi_labels", "temperature_cards", "kpi_text_has_temp", "page_has_forbidden")
        },
    }


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
    # 短断流（预算内）：生产 VideoCard 有界重连预算 = ICE 判定断开（实测 ~7s）后 1s/2s/4s 三次，
    # 即断流后约 14s 内源必须恢复。此处 3s 后恢复（加发布端 ready 2-4s），确定性落在预算内，
    # 严格断言 recovered=auto。超出预算的长断流见 test_manual_recovery_after_long_outage。
    outage_started = time.monotonic()
    time.sleep(3)
    _publish(web, RGB, "rgb")  # 恢复
    assert wait_for(lambda: _ready(web, RGB), 30)
    outage_seconds = round(time.monotonic() - outage_started, 1)
    restored_file.write_text("restored")
    proc.wait(timeout=90)
    out, err = proc.stdout.read(), proc.stderr.read()
    data = json.loads(out.strip().splitlines()[-1])
    assert data.get("ok"), _redact(json.dumps(data, ensure_ascii=False)[-800:] + err[-800:])
    data["outage_seconds"] = outage_seconds
    EVIDENCE["reconnect"] = data
    assert data["state_during_outage"], data  # 断流期间 VideoCard 离开 LIVE（不伪装在线）
    assert data["violations"] == 0, data
    # 严格自动重连：浏览器侧只点过一次「连接视频」（初始连接），恢复必须是 auto
    assert data["clicks_during_reconnect"] == 1, data
    assert data["cards"]["rgb"]["recovered"] == "auto", data
    assert data["cards"]["rgb"]["decoded_after"], data


def test_simulation_never_writes_real_001_sensor_facts(web):
    # 浏览器验收全程（含 1 次兼容温度注入）后，真实车辆 SensorSample 计数不变，
    # Redis 中真实车辆 latest 不含任何模拟温度值
    assert _real_sensor_counts() == web["real_before"]
    for vid in REAL_VEHICLES:
        raw = web["redis"].get(f"robot:{vid}:latest")
        if raw:
            assert json.loads(raw).get("top_ir") not in (27.5, 42.0, 67.8), vid
    EVIDENCE["real_isolation"] = {"real_sensor_counts": web["real_before"], "unchanged": True}


def _spawn_browser(web, phase: str, payload: dict):
    proc = subprocess.Popen(
        [
            "node",
            str(BROWSER / "web_videocard.mjs"),
            web["base_url"],
            SIM,
            str(_evidence_dir()),
            phase,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CHROMIUM_PATH": web["chromium"]},
    )
    username, password = _admin_credentials()
    proc.stdin.write(json.dumps({"username": username, "password": password, **payload}))
    proc.stdin.close()
    return proc


def _finish(proc, timeout):
    proc.wait(timeout=timeout)
    out, err = proc.stdout.read(), proc.stderr.read()
    data = json.loads(out.strip().splitlines()[-1])
    assert data.get("ok"), _redact(json.dumps(data, ensure_ascii=False)[-800:] + err[-800:])
    return data


def test_dual_stream_60s_continuous(web):
    """RGB + thermal 两张生产 VideoCard 同时连续播放 60s。

    每秒都有新解码帧、画面持续变化、始终实时。
    """
    for stream_id, kind in ((RGB, "rgb"), (THERMAL, "thermal")):
        if not _ready(web, stream_id):
            _publish(web, stream_id, kind)
            assert wait_for(lambda s=stream_id: _ready(web, s), 30)
    data = _browser(web, "dual60", {"seconds": 60}, timeout=150)
    EVIDENCE["dual60"] = {
        "stats": data["dual_stats"],
        "gates": data["gates"],
        "timeline": data["dual"],
    }
    assert len(data["dual"]) == 60
    for stat in data["dual_stats"]:
        assert stat["zero_fps_seconds"] == 0, stat  # 60 个 1 秒窗口均有新帧
        assert stat["min_fps_1s"] >= 5, stat
        assert stat["total_frames"] >= 600, stat
        assert stat["distinct_hashes"] >= 50, stat  # 画面持续变化（非静帧）
        assert stat["non_live_seconds"] == 0, stat
    for gate in data["gates"]:
        assert gate["violation"] is False, gate


def test_manual_recovery_after_long_outage(web):
    """长时间断流：有界自动重连耗尽 → 明确提示；源恢复后不自行恢复；手动「连接视频」后解码恢复。"""
    ready = web["workdir"] / "mn-ready"
    exhausted = web["workdir"] / "mn-exhausted"
    restored = web["workdir"] / "mn-restored"
    for f in (ready, exhausted, restored):
        f.unlink(missing_ok=True)
    if not _ready(web, RGB):
        _publish(web, RGB, "rgb")
        assert wait_for(lambda: _ready(web, RGB), 30)
    proc = _spawn_browser(
        web,
        "manual",
        {
            "ready_file": str(ready),
            "exhausted_file": str(exhausted),
            "restored_file": str(restored),
        },
    )
    assert wait_for(ready.exists, 60), "browser never connected"
    stop(web["publishers"].pop(RGB))
    assert wait_for(exhausted.exists, 100), "auto-reconnect never exhausted"
    _publish(web, RGB, "rgb")
    assert wait_for(lambda: _ready(web, RGB), 30)
    restored.write_text("restored")
    data = _finish(proc, 120)
    EVIDENCE["manual"] = data
    assert data["exhausted"] and "自动重连失败" in data["exhausted"]["text"], data
    assert data["auto_after_exhausted"] is False, data  # 耗尽后不得无点击自行恢复
    assert data["clicks_before_manual"] == 1, data
    assert data["manual"]["decoded_after"], data


def test_vehicle_switch_releases_sim_session_and_real_001_shows_no_sim(web):
    """模拟设备双路播放 → 设备页切换到真实 001。

    旧 WHEP 会话 DELETE；真实 001 页面无模拟横幅/模拟流。
    """
    for stream_id, kind in ((RGB, "rgb"), (THERMAL, "thermal")):
        if not _ready(web, stream_id):
            _publish(web, stream_id, kind)
            assert wait_for(lambda s=stream_id: _ready(web, s), 30)
    data = _browser(web, "switch", {"target": "firebot-vehicle-01"})
    EVIDENCE["switch"] = data
    assert data["navigation"] == "spa"
    assert data["before_switch"]["banner"] == 1
    after = data["after_switch"]
    assert after["stored_vehicle"] == "firebot-vehicle-01", after
    assert after["banner"] == 0, after
    assert after["sim_deletes"] == 2, after  # 两路模拟旧会话在组件卸载时 DELETE 释放
    assert after["sim_requests_after"] == 0, after
    for c in after["cards"]:
        assert c["live"] is False and c["video_src"] is False, c
    assert data["page_text_has_sim"] is False
