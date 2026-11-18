"""视频源 provider 与 ffmpeg 命令构造。

provider（Hardware Profile 选择；同型车默认 http_mjpeg）：
  http_mjpeg  复用车内 web_video_server（/stream?topic=<t>&type=mjpeg）。相机只被 ROS 驱动
              采集一次；局域网 web_nav 与云端发布器都是 web_video_server 的 HTTP 消费者，
              互不抢占 USB 设备（单次采集、多消费者）。
  v4l2        可选：直接打开 /dev/v4l/by-id/...（仅当车内无 ROS 采集节点持有该设备时）。
              设备被占用（EBUSY）→ DEVICE_BUSY，长退避，绝不与 ROS 争用。
  rtsp        可选：网络相机。URL（含凭证）只经 0600 文件 → ffconcat 临时文件（0600，
              媒体用户私有 PrivateTmp）传给 ffmpeg，绝不出现在 argv；先 ffprobe 验证实际编码，
              仅 H.264 + yuv420p/yuvj420p 才 -c copy，否则转码（草稿未验证即 copy）。

编码器（FFmpeg 4.2.x 实际可用参数，经实机 4.2.7 验证）：
  h264_nvenc  -preset llhp -zerolatency 1（4.2 无 p1~p7 / -tune）
  h264_vaapi  -vaapi_device <render> -vf format=nv12,hwupload
  libx264     -preset veryfast -tune zerolatency
  auto        依次对 nvenc → vaapi → libx264 做 1 帧真实试编码，取第一个成功者。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import List, Optional, Tuple

from .config import ConfigError, MediaConfig, read_secret_file

FFMPEG = os.environ.get("FIREBOT_MEDIA_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FIREBOT_MEDIA_FFPROBE", "ffprobe")


def _encoder_args(encoder: str, cfg: MediaConfig) -> Tuple[List[str], List[str]]:
    """返回 (输入前置参数, 输出编码参数)。"""
    cam = cfg.camera
    gop = str(cam.fps * cam.gop_seconds)
    rate = "%dk" % cam.bitrate_kbps
    common = [
        "-g",
        gop,
        "-bf",
        "0",
        "-b:v",
        rate,
        "-maxrate",
        rate,
        "-bufsize",
        "%dk" % (cam.bitrate_kbps * 2),
    ]
    if encoder == "h264_nvenc":
        return [], [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "llhp",
            "-zerolatency",
            "1",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
        ] + common
    if encoder == "h264_vaapi":
        return ["-vaapi_device", cfg.vaapi_device], [
            "-c:v",
            "h264_vaapi",
            "-profile:v",
            "main",
        ] + common
    return [], [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
    ] + common


def _filters(encoder: str, cfg: MediaConfig) -> List[str]:
    cam = cfg.camera
    chain = ["fps=%d" % cam.fps]
    if cam.width and cam.height:
        chain.append("scale=%d:%d" % (cam.width, cam.height))
    if encoder == "h264_vaapi":
        chain += ["format=nv12", "hwupload"]
    else:
        chain.append("format=yuv420p")
    return ["-vf", ",".join(chain)]


def probe_encoder(encoder: str, cfg: MediaConfig, timeout: float = 20.0) -> bool:
    """真实试编码 1 帧（证明该编码器在本机实际可用，而非仅“已编译”）。"""
    pre, enc = _encoder_args(encoder, cfg)
    cmd = (
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error"]
        + pre
        + ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=%d" % cfg.camera.fps, "-frames:v", "3"]
        + _filters(encoder, cfg)
        + enc
        + ["-f", "null", "-"]
    )
    try:
        return (
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_encoder(cfg: MediaConfig) -> str:
    if cfg.encoder != "auto":
        if not probe_encoder(cfg.encoder, cfg):
            raise ConfigError(
                "指定编码器 %s 试编码失败（驱动/设备不可用或参数不被本机 FFmpeg 支持）"
                % cfg.encoder
            )
        return cfg.encoder
    for enc in ("h264_nvenc", "h264_vaapi", "libx264"):
        if probe_encoder(enc, cfg):
            return enc
    raise ConfigError("无可用 H.264 编码器（nvenc/vaapi/libx264 试编码全部失败）")


class RtspInput:
    """把 RTSP URL 写入 0600 ffconcat 文件，ffmpeg 经 -f concat 读取（URL 不进 argv）。"""

    def __init__(self, cfg: MediaConfig) -> None:
        self.url = read_secret_file(cfg.camera.rtsp_url_file)
        fd, self.path = tempfile.mkstemp(prefix="firebot-media-", suffix=".ffconcat")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("ffconcat version 1.0\nfile '%s'\n" % self.url.replace("'", ""))
        os.chmod(self.path, 0o600)

    def args(self) -> List[str]:
        # FFmpeg 4.2 concat 不支持逐文件 option（4.4 起才有），也不向内层 rtsp 透传 -rtsp_transport；
        # RTSP 协商由 ffmpeg 自动回退（UDP 不可用 → TCP），实测 4.2.7 可正常取流。
        return [
            "-f",
            "concat",
            "-safe",
            "0",
            "-protocol_whitelist",
            "file,rtsp,rtp,udp,tcp,tls",
            "-i",
            self.path,
        ]

    def close(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


def probe_rtsp_codec(rtsp: RtspInput, timeout: float = 15.0) -> Optional[dict]:
    cmd = (
        [FFPROBE, "-hide_banner", "-v", "error"]
        + rtsp.args()
        + [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,profile",
            "-of",
            "json",
        ]
    )
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        streams = json.loads(out.stdout.decode("utf-8", "replace")).get("streams") or []
    except ValueError:
        return None
    return streams[0] if streams else None


def build_command(
    cfg: MediaConfig,
    encoder: str,
    rtsp: Optional[RtspInput] = None,
    rtsp_codec: Optional[dict] = None,
) -> Tuple[List[str], bool]:
    """返回 (ffmpeg argv, is_copy)。argv 不含任何 secret（rtsp 走 ffconcat 文件）。"""
    cam = cfg.camera
    base = [FFMPEG, "-hide_banner", "-nostdin", "-v", "warning", "-nostats"]
    if cam.provider == "http_mjpeg":
        inp = [
            "-use_wallclock_as_timestamps",
            "1",
            "-rw_timeout",
            str(int(cam.stall_timeout_s * 1e6)),
            "-f",
            "mpjpeg",
            "-i",
            cam.http_url,
        ]
    elif cam.provider == "v4l2":
        inp = ["-f", "v4l2"]
        if cam.input_format:
            inp += ["-input_format", cam.input_format]
        if cam.width and cam.height:
            inp += ["-video_size", "%dx%d" % (cam.width, cam.height)]
        inp += ["-framerate", str(cam.fps), "-i", cam.device]
    else:
        assert rtsp is not None
        inp = rtsp.args()
    copy = bool(
        cam.provider == "rtsp"
        and rtsp_codec
        and rtsp_codec.get("codec_name") == "h264"
        and rtsp_codec.get("pix_fmt") in ("yuv420p", "yuvj420p")
    )
    out = [
        "-an",
        "-f",
        "mpegts",
        "-mpegts_flags",
        "+resend_headers",
        "-muxdelay",
        "0",
        "-flush_packets",
        "1",
        "pipe:1",
    ]
    if copy:
        return base + inp + ["-c:v", "copy", "-bsf:v", "dump_extra"] + out, True
    pre, enc = _encoder_args(encoder, cfg)
    return base + pre + inp + _filters(encoder, cfg) + enc + out, False
