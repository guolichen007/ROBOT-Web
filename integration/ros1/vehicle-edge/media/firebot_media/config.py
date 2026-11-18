"""媒体发布器配置加载与校验（fail-closed）。

配置来源（全部在 Edge Home，release 之外；车辆只是数据）：
  config/media-hardware.env   Hardware Profile（同型车共用；install 时由仓库 profile 落位）
  config/media.env            车辆/站点覆盖（SRT endpoint、vehicle_id；凭证导入时生成）
  config/identity.env         Enrollment 身份（存在时必须与 media vehicle_id 一致）
  media/secrets/srt_publish_token       per-vehicle 发布 token（0600，媒体用户所有）
  media/secrets/srt_publish_passphrase  平台共享 SRT 链路加密 passphrase（0600）
  media/secrets/rtsp_<camera>.url       仅 rtsp provider（0600；含相机凭证）

任何永久性配置错误抛 ConfigError → 进程以 EX_CONFIG(78) 退出，systemd
RestartPreventExitStatus=78 不再重启（避免刷屏）；可恢复故障在进程内退避重连。
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Optional

EX_CONFIG = 78

CAMERA_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
VEHICLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDERS = ("http_mjpeg", "v4l2", "rtsp")
ENCODERS = ("auto", "libx264", "h264_nvenc", "h264_vaapi")
# 不稳定的内核枚举名（重启/插拔顺序变化即错位）：v4l2 provider 只接受 by-id / by-path
UNSTABLE_DEVICE_RE = re.compile(r"^/dev/video[0-9]+$")


class ConfigError(Exception):
    """永久性配置错误（不可通过重试恢复）。消息不得包含任何 secret 值。"""


def load_env_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            data[key] = value.strip().strip('"').strip("'")
    return data


def read_secret_file(path: str, *, owner_uid: Optional[int] = None) -> str:
    """读取 secret 文件：必须是普通文件、group/other 无任何权限；内容 strip（与 API 一致）。"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise ConfigError("secret 文件缺失: %s" % path) from None
    except PermissionError:
        raise ConfigError("secret 文件不可访问（权限）: %s" % path) from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError("secret 不是普通文件: %s" % path)
    if st.st_mode & 0o077:
        raise ConfigError(
            "secret 权限过宽（必须 0600/0400）: %s mode=%o" % (path, st.st_mode & 0o777)
        )
    if owner_uid is not None and st.st_uid not in (owner_uid, 0):
        raise ConfigError("secret 属主异常: %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except PermissionError:
        raise ConfigError("secret 文件不可读（运行用户无权限）: %s" % path) from None
    if not value:
        raise ConfigError("secret 文件为空: %s" % path)
    return value


@dataclass
class CameraConfig:
    camera: str
    provider: str
    fps: int
    bitrate_kbps: int
    gop_seconds: int
    width: int = 0
    height: int = 0
    http_url: str = ""
    device: str = ""
    input_format: str = ""
    rtsp_url_file: str = ""
    stall_timeout_s: float = 10.0


@dataclass
class MediaConfig:
    edge_home: str
    vehicle_id: str
    srt_host: str
    srt_port: int
    srt_latency_ms: int
    encoder: str
    camera: CameraConfig
    secrets_dir: str
    state_dir: str
    token: str = field(repr=False, default="")
    passphrase: str = field(repr=False, default="")
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 60.0
    auth_backoff_s: float = 120.0
    log_lines_per_minute: int = 30
    vaapi_device: str = "/dev/dri/renderD128"

    @property
    def stream_id(self) -> str:
        return "%s-%s" % (self.vehicle_id, self.camera.camera)

    @property
    def streamid(self) -> str:
        """MediaMTX SRT streamid：publish:<path>:<user>:<pass>（只存在进程内存）。"""
        return "publish:%s:%s:%s" % (self.stream_id, self.vehicle_id, self.token)

    def secret_values(self) -> List[str]:
        return [v for v in (self.token, self.passphrase, self.streamid) if v]


def _int(env: Dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "")
    if raw == "":
        return default
    if not re.fullmatch(r"[0-9]+", raw):
        raise ConfigError("%s 必须是整数" % key)
    value = int(raw)
    if not lo <= value <= hi:
        raise ConfigError("%s 超出范围 [%d,%d]" % (key, lo, hi))
    return value


def load_config(edge_home: str, camera: str, *, load_secrets: bool = True) -> MediaConfig:
    if not CAMERA_RE.fullmatch(camera):
        raise ConfigError("camera 名称非法: %r" % camera)
    cfg_dir = os.path.join(edge_home, "config")
    env: Dict[str, str] = {}
    hw = os.path.join(cfg_dir, "media-hardware.env")
    site = os.path.join(cfg_dir, "media.env")
    if not os.path.isfile(hw):
        raise ConfigError("缺少 Hardware Profile: config/media-hardware.env（先 media install）")
    if not os.path.isfile(site):
        raise ConfigError("缺少 config/media.env（先 media credentials import）")
    env.update(load_env_file(hw))
    env.update(load_env_file(site))

    cameras = [c for c in env.get("FIREBOT_MEDIA_CAMERAS", "").split(",") if c]
    if camera not in cameras:
        raise ConfigError("camera %s 未在 FIREBOT_MEDIA_CAMERAS 中启用" % camera)

    vehicle_id = env.get("FIREBOT_MEDIA_VEHICLE_ID", "")
    if not VEHICLE_RE.fullmatch(vehicle_id):
        raise ConfigError("FIREBOT_MEDIA_VEHICLE_ID 缺失或非法")
    identity = os.path.join(cfg_dir, "identity.env")
    if os.path.isfile(identity):
        enrolled = load_env_file(identity).get("DEVICE_ID", "")
        if enrolled and enrolled != vehicle_id:
            raise ConfigError("媒体身份与 Enrollment 身份不一致（identity.env DEVICE_ID）")

    srt_host = env.get("FIREBOT_MEDIA_SRT_HOST", "")
    if not HOST_RE.fullmatch(srt_host) or "REPLACE" in srt_host or "<" in srt_host:
        raise ConfigError("FIREBOT_MEDIA_SRT_HOST 缺失或非法")
    srt_port = _int(env, "FIREBOT_MEDIA_SRT_PORT", 0, 1, 65535)
    encoder = env.get("FIREBOT_MEDIA_ENCODER", "auto")
    if encoder not in ENCODERS:
        raise ConfigError("FIREBOT_MEDIA_ENCODER 非法: %s" % encoder)

    prefix = "FIREBOT_MEDIA_%s_" % camera.upper()
    provider = env.get(prefix + "PROVIDER", "")
    if provider not in PROVIDERS:
        raise ConfigError("%sPROVIDER 缺失或非法（%s）" % (prefix, "/".join(PROVIDERS)))
    cam = CameraConfig(
        camera=camera,
        provider=provider,
        fps=_int(env, prefix + "FPS", 15, 1, 60),
        bitrate_kbps=_int(env, prefix + "BITRATE_KBPS", 1500, 100, 20000),
        gop_seconds=_int(env, prefix + "GOP_SECONDS", 2, 1, 10),
        width=_int(env, prefix + "WIDTH", 0, 0, 7680),
        height=_int(env, prefix + "HEIGHT", 0, 0, 4320),
        stall_timeout_s=float(_int(env, prefix + "STALL_TIMEOUT_S", 10, 3, 120)),
    )
    if provider == "http_mjpeg":
        url = env.get(prefix + "HTTP_URL", "")
        if not re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?/[^\s'\"]*", url):
            # 只允许本机 web_video_server：拒绝把局域网/公网地址当作本车相机
            raise ConfigError("%sHTTP_URL 必须是本机 http://127.0.0.1:<port>/... " % prefix)
        cam.http_url = url
    elif provider == "v4l2":
        device = env.get(prefix + "DEVICE", "")
        if not device.startswith("/dev/"):
            raise ConfigError("%sDEVICE 缺失" % prefix)
        if UNSTABLE_DEVICE_RE.fullmatch(device):
            raise ConfigError(
                "%sDEVICE=%s 为不稳定枚举名；必须使用 /dev/v4l/by-id/ 或 /dev/v4l/by-path/"
                % (prefix, device)
            )
        cam.device = device
        cam.input_format = env.get(prefix + "INPUT_FORMAT", "")
        if cam.input_format and not re.fullmatch(r"[a-z0-9_]{1,16}", cam.input_format):
            raise ConfigError("%sINPUT_FORMAT 非法" % prefix)
    else:
        cam.rtsp_url_file = os.path.join(edge_home, "media", "secrets", "rtsp_%s.url" % camera)

    cfg = MediaConfig(
        edge_home=edge_home,
        vehicle_id=vehicle_id,
        srt_host=srt_host,
        srt_port=srt_port,
        srt_latency_ms=_int(env, "FIREBOT_MEDIA_SRT_LATENCY_MS", 200, 20, 8000),
        encoder=encoder,
        camera=cam,
        secrets_dir=os.path.join(edge_home, "media", "secrets"),
        state_dir=os.path.join(edge_home, "media", "state"),
        backoff_initial_s=float(_int(env, "FIREBOT_MEDIA_BACKOFF_INITIAL_S", 2, 1, 60)),
        backoff_max_s=float(_int(env, "FIREBOT_MEDIA_BACKOFF_MAX_S", 60, 2, 600)),
        auth_backoff_s=float(_int(env, "FIREBOT_MEDIA_AUTH_BACKOFF_S", 120, 2, 3600)),
        log_lines_per_minute=_int(env, "FIREBOT_MEDIA_LOG_LINES_PER_MINUTE", 30, 5, 600),
        vaapi_device=env.get("FIREBOT_MEDIA_VAAPI_DEVICE", "/dev/dri/renderD128"),
    )
    if load_secrets:
        cfg.token = read_secret_file(os.path.join(cfg.secrets_dir, "srt_publish_token"))
        if not TOKEN_RE.fullmatch(cfg.token):
            raise ConfigError("srt_publish_token 格式异常（期望 64 位 hex）")
        cfg.passphrase = read_secret_file(os.path.join(cfg.secrets_dir, "srt_publish_passphrase"))
        if not 10 <= len(cfg.passphrase.encode("utf-8")) <= 79:
            raise ConfigError("srt_publish_passphrase 长度必须 10..79 字节（SRT 限制）")
        if provider == "rtsp":
            url = read_secret_file(cam.rtsp_url_file)
            if not url.startswith("rtsp://") or any(c in url for c in "\n'\""):
                raise ConfigError("rtsp_%s.url 格式非法" % camera)
    return cfg
