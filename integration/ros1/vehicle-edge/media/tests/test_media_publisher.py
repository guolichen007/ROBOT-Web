#!/usr/bin/env python3
"""车端媒体发布器单元/行为测试（CI 可跑：仅需 python3 + ffmpeg；不需要 libsrt / 相机 / 网络）。

覆盖草稿 1c4bf74 独立审查的 10 项缺陷中可离线证明的部分（运行实证见 integration/g4-fleet-media-e2e）：
  D1 双路独立发布路径（stream_id = <vehicle_id>-<camera>，streamid 按 camera 生成）
  D3 默认 provider = http_mjpeg（不直开 USB；与 ROS 采集不互斥）
  D4 secret 文件权限：group/other 任何位 → 拒绝；不可读 → CONFIG_ERROR（非静默）
  D5 /dev/videoN 不稳定枚举名拒绝，只接受 /dev/v4l/by-id|by-path
  D6 RTSP URL 不进 argv（ffconcat 0600 文件）；日志脱敏
  D7 RTSP 仅实测 H.264+yuv420p 才 copy
  D8 FFmpeg 4.2 编码参数（无 -preset p1 / -tune ll）
  D10 日志限速
  退出码：配置错误 78；无 srt_publish_token → 78
用法：python3 tests/test_media_publisher.py
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MEDIA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEDIA))

from firebot_media import config as C  # noqa: E402
from firebot_media import sources as S  # noqa: E402
from firebot_media.log import Logger  # noqa: E402

FAILURES: list = []
TOKEN = "a" * 64
PASS = "platform-shared-srt-pass"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(detail)))
    if not cond:
        FAILURES.append(name)


def edge_home(tmp: Path, extra: str = "", vehicle="firebot-vehicle-99") -> Path:
    home = tmp / "edge"
    (home / "config").mkdir(parents=True)
    sec = home / "media" / "secrets"
    sec.mkdir(parents=True)
    hw = (MEDIA / "profiles" / "firebot_ros1_standard_v1.media.env").read_text()
    (home / "config/media-hardware.env").write_text(hw)
    (home / "config/media.env").write_text(
        "FIREBOT_MEDIA_VEHICLE_ID=%s\nFIREBOT_MEDIA_SRT_HOST=media.example.test\n"
        "FIREBOT_MEDIA_SRT_PORT=8890\n%s" % (vehicle, extra)
    )
    for name, value in (("srt_publish_token", TOKEN), ("srt_publish_passphrase", PASS)):
        (sec / name).write_text(value + "\n")
        os.chmod(sec / name, 0o600)
    return home


def expect_config_error(name, fn):
    try:
        fn()
    except C.ConfigError as exc:
        check(name, True)
        return str(exc)
    check(name, False, "no ConfigError")
    return ""


def test_dual_stream_identity():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        rgb = C.load_config(str(home), "roof_rgb")
        th = C.load_config(str(home), "roof_thermal")
        check(
            "D1 stream_id per camera",
            rgb.stream_id == "firebot-vehicle-99-roof_rgb"
            and th.stream_id == "firebot-vehicle-99-roof_thermal",
        )
        check(
            "D1 streamid 不同且绑定车辆身份",
            rgb.streamid != th.streamid
            and rgb.streamid == "publish:firebot-vehicle-99-roof_rgb:firebot-vehicle-99:" + TOKEN,
        )
        check(
            "D3 默认 provider=http_mjpeg（复用 web_video_server）",
            rgb.camera.provider == th.camera.provider == "http_mjpeg",
        )
        check(
            "D3 RGB 话题 = /camera/color/image_raw（RealSense，非双目拼接）",
            "topic=/camera/color/image_raw" in rgb.camera.http_url,
        )
        check("D3 thermal 话题 = /thermal/image", "topic=/thermal/image" in th.camera.http_url)
        check("repr 不含 secret", TOKEN not in repr(rgb) and PASS not in repr(rgb))


def test_secret_permissions():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        tok = home / "media/secrets/srt_publish_token"
        os.chmod(tok, 0o640)
        msg = expect_config_error(
            "D4 token 0640 → 拒绝", lambda: C.load_config(str(home), "roof_rgb")
        )
        check("D4 错误消息不含 token 值", TOKEN not in msg)
        os.chmod(tok, 0o600)
        tok.write_text("not-hex\n")
        os.chmod(tok, 0o600)
        expect_config_error("token 格式异常 → 拒绝", lambda: C.load_config(str(home), "roof_rgb"))
        tok.unlink()
        expect_config_error("token 缺失 → 拒绝", lambda: C.load_config(str(home), "roof_rgb"))
        if os.geteuid() != 0:
            tok.write_text(TOKEN)
            os.chmod(tok, 0o000)
            msg = expect_config_error(
                "D4 token 不可读（权限）→ 明确 CONFIG_ERROR",
                lambda: C.load_config(str(home), "roof_rgb"),
            )
            check("D4 不可读消息可诊断", "权限" in msg or "不可" in msg, msg)
            os.chmod(tok, 0o600)


def test_identity_and_endpoint_validation():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        (home / "config/identity.env").write_text("DEVICE_ID=firebot-vehicle-98\n")
        expect_config_error(
            "媒体身份 ≠ Enrollment 身份 → 拒绝", lambda: C.load_config(str(home), "roof_rgb")
        )
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t), extra="FIREBOT_MEDIA_SRT_HOST=<云端 SRT 公网 host，待交付>\n")
        expect_config_error("占位 SRT host → 拒绝", lambda: C.load_config(str(home), "roof_rgb"))
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(
            Path(t),
            extra="FIREBOT_MEDIA_ROOF_RGB_HTTP_URL=http://192.168.1.50:8080/stream?topic=/x\n",
        )
        expect_config_error(
            "http_mjpeg 非本机 URL → 拒绝", lambda: C.load_config(str(home), "roof_rgb")
        )
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        expect_config_error("未启用 camera → 拒绝", lambda: C.load_config(str(home), "bottom_ir"))
        expect_config_error("非法 camera 名 → 拒绝", lambda: C.load_config(str(home), "../x"))


def test_v4l2_stable_device():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(
            Path(t),
            extra="FIREBOT_MEDIA_ROOF_THERMAL_PROVIDER=v4l2\n"
            "FIREBOT_MEDIA_ROOF_THERMAL_DEVICE=/dev/video1\n",
        )
        expect_config_error(
            "D5 /dev/video1 不稳定枚举名 → 拒绝", lambda: C.load_config(str(home), "roof_thermal")
        )
    with tempfile.TemporaryDirectory() as t:
        dev = "/dev/v4l/by-id/usb-SV_Thermal_0001-video-index0"
        home = edge_home(
            Path(t),
            extra="FIREBOT_MEDIA_ROOF_THERMAL_PROVIDER=v4l2\n"
            "FIREBOT_MEDIA_ROOF_THERMAL_DEVICE=%s\n"
            "FIREBOT_MEDIA_ROOF_THERMAL_INPUT_FORMAT=yuyv422\n" % dev,
        )
        cfg = C.load_config(str(home), "roof_thermal")
        cmd, copy = S.build_command(cfg, "libx264")
        check("D5 by-id 设备接受并进入 -i", dev in cmd and "v4l2" in cmd and not copy)


def test_rtsp_no_argv_and_codec_gate():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t), extra="FIREBOT_MEDIA_ROOF_RGB_PROVIDER=rtsp\n")
        url = "rtsp://admin:Cam-P4ss-xyz@192.168.10.20:554/Streaming/Channels/101"
        f = home / "media/secrets/rtsp_roof_rgb.url"
        f.write_text(url + "\n")
        os.chmod(f, 0o600)
        cfg = C.load_config(str(home), "roof_rgb")
        rtsp = S.RtspInput(cfg)
        try:
            mode = stat.S_IMODE(os.stat(rtsp.path).st_mode)
            check("D6 ffconcat 临时文件 0600", mode == 0o600, oct(mode))
            for codec, want_copy in (
                ({"codec_name": "h264", "pix_fmt": "yuv420p"}, True),
                ({"codec_name": "h264", "pix_fmt": "yuvj420p"}, True),
                ({"codec_name": "hevc", "pix_fmt": "yuv420p"}, False),
                ({"codec_name": "h264", "pix_fmt": "yuv422p"}, False),
            ):
                cmd, copy = S.build_command(cfg, "libx264", rtsp, codec)
                argv = " ".join(cmd)
                check(
                    "D6 argv 无 RTSP 凭证/URL（%s/%s）" % (codec["codec_name"], codec["pix_fmt"]),
                    "Cam-P4ss" not in argv and "rtsp://" not in argv,
                )
                check(
                    "D7 copy 仅当实测 H.264+420（%s/%s → copy=%s）"
                    % (codec["codec_name"], codec["pix_fmt"], want_copy),
                    copy is want_copy,
                )
            cmd, copy = S.build_command(cfg, "libx264", rtsp, None)
            check("D7 未知编码 → 不 copy", copy is False)
        finally:
            rtsp.close()
        check("ffconcat 临时文件清理", not os.path.exists(rtsp.path))


def test_ffmpeg42_encoder_args():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        cfg = C.load_config(str(home), "roof_rgb")
        nv = " ".join(S.build_command(cfg, "h264_nvenc")[0])
        check(
            "D8 nvenc 不含 4.2 不支持的 -preset p1 / -tune",
            "p1" not in nv.split() and "-tune" not in nv,
        )
        check(
            "D8 nvenc 使用 4.2 支持的 llhp + zerolatency",
            "-preset llhp" in nv and "-zerolatency 1" in nv,
        )
        va = S.build_command(cfg, "h264_vaapi")[0]
        check(
            "D8 vaapi hwupload + nv12", "-vaapi_device" in va and any("hwupload" in a for a in va)
        )
        x = " ".join(S.build_command(cfg, "libx264")[0])
        check(
            "输出 H.264 yuv420p + mpegts 到 stdout（无 udp 回环端口）",
            "-pix_fmt yuv420p" in x
            and "-f mpegts" in x
            and x.endswith("pipe:1")
            and "udp://" not in x,
        )
        check("B 帧关闭 + GOP=fps*gop_s", "-bf 0" in x and "-g 30" in x)


def test_ffmpeg_real_encode_mjpeg_input():
    """真实运行：MJPEG multipart（web_video_server 格式）→ build_command → H.264 TS（需 ffmpeg）。"""
    ff = os.environ.get("FIREBOT_MEDIA_FFMPEG", "ffmpeg")
    if subprocess.run([ff, "-version"], capture_output=True).returncode != 0:
        print("SKIP ffmpeg 不可用（真实编码段 NOT_RUN）")
        return
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        mj = tmp / "src.mjpeg"
        subprocess.run(
            [
                ff,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=10",
                "-t",
                "2",
                "-c:v",
                "mjpeg",
                "-f",
                "mpjpeg",
                str(mj),
            ],
            check=True,
        )
        home = edge_home(tmp)
        cfg = C.load_config(str(home), "roof_thermal")
        cmd, _ = S.build_command(cfg, "libx264")
        i = cmd.index("-i")
        cmd[i + 1] = str(mj)
        for opt in ("-rw_timeout",):
            if opt in cmd:
                k = cmd.index(opt)
                del cmd[k : k + 2]
        out = subprocess.run(cmd, capture_output=True, timeout=60)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,pix_fmt",
                "-of",
                "csv=p=0",
                "-",
            ],
            input=out.stdout,
            capture_output=True,
        )
        check(
            "真实编码 MJPEG → H.264/yuv420p TS",
            out.returncode == 0 and b"h264,yuv420p" in probe.stdout,
            out.stderr[-200:],
        )


def test_logger_redact_and_ratelimit():
    buf = io.StringIO()
    log = Logger("roof_rgb", lines_per_minute=5, stream=buf)
    log.add_secrets([TOKEN, PASS])
    log.log("INFO", "connect srt://h:8890?streamid=publish:x:y:%s&passphrase=%s" % (TOKEN, PASS))
    log.log("INFO", "rtsp://admin:Cam-P4ss@10.0.0.2/stream failed")
    out = buf.getvalue()
    check(
        "D6 日志脱敏（token/passphrase/streamid/rtsp 凭证）",
        TOKEN not in out and PASS not in out and "Cam-P4ss" not in out and "publish:x" not in out,
        out,
    )
    for i in range(50):
        log.log("WARN", "spam %d" % i)
    lines = buf.getvalue().strip().splitlines()
    check("D10 日志限速（≤5 行/分钟）", len(lines) <= 5, len(lines))


def test_exit_code_config_error():
    with tempfile.TemporaryDirectory() as t:
        home = edge_home(Path(t))
        (home / "media/secrets/srt_publish_token").unlink()
        env = dict(os.environ, PYTHONPATH=str(MEDIA))
        py = os.environ.get("FIREBOT_MEDIA_TEST_PYTHON", sys.executable)
        r = subprocess.run(
            [py, "-m", "firebot_media", "roof_rgb", "--edge-home", str(home)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        check(
            "D2 永久配置错误 → 退出码 78（RestartPreventExitStatus）",
            r.returncode == 78,
            r.returncode,
        )
        check("D2 非 0（旧 wait -n || true 会把失败变成 0）", r.returncode != 0)


def test_systemd_unit_semantics():
    unit = (MEDIA / "systemd" / "firebot-media@.service.template").read_text()
    active = [
        ln.strip() for ln in unit.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    check(
        "D9 StartLimitIntervalSec=0（长期断网不进入 start-limit-hit）",
        "StartLimitIntervalSec=0" in active,
    )
    check(
        "D2 RestartPreventExitStatus=78 + Restart=always",
        "RestartPreventExitStatus=78" in active and "Restart=always" in active,
    )
    check(
        "D10 journald 限速 + 无文件日志",
        "LogRateLimitBurst=120" in active
        and not any(">" in ln or ".log" in ln for ln in active if ln.startswith("ExecStart")),
    )
    check(
        "低权限：User=@MEDIA_USER@ + NoNewPrivileges",
        "User=@MEDIA_USER@" in active and "NoNewPrivileges=yes" in active,
    )
    check("KillMode=control-group（无孤儿）", "KillMode=control-group" in active)
    check(
        "与 G1/edge/ROS 无依赖（不 Requires/BindsTo/PartOf）",
        not any(ln.startswith(("Requires=", "BindsTo=", "PartOf=")) for ln in active),
    )
    check(
        "ExecStart 无 secret 参数",
        all(
            "token" not in ln and "passphrase" not in ln
            for ln in active
            if ln.startswith("ExecStart")
        ),
    )


def test_no_motion_control_paths():
    src = "".join(p.read_text() for p in (MEDIA / "firebot_media").glob("*.py"))
    check(
        "不新增任何运动控制路径（cmd_vel/move_base/web_control/rosbridge 9090）",
        not any(
            k in src for k in ("cmd_vel", "move_base", "/web_control", ":9090", "rospy", "e_stop")
        ),
    )


def main() -> int:
    for fn in (
        test_dual_stream_identity,
        test_secret_permissions,
        test_identity_and_endpoint_validation,
        test_v4l2_stable_device,
        test_rtsp_no_argv_and_codec_gate,
        test_ffmpeg42_encoder_args,
        test_ffmpeg_real_encode_mjpeg_input,
        test_logger_redact_and_ratelimit,
        test_exit_code_config_error,
        test_systemd_unit_semantics,
        test_no_motion_control_paths,
    ):
        fn()
    if FAILURES:
        print("\nMEDIA_PUBLISHER_TEST=FAIL count=%d" % len(FAILURES))
        return 1
    print("\nMEDIA_PUBLISHER_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
