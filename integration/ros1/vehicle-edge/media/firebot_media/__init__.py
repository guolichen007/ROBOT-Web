"""Firebot 车端统一媒体发布器（Vehicle Edge media component）。

一路相机 = 一个进程（systemd firebot-media@<camera>.service）：
  source provider（默认 http_mjpeg：复用 web_video_server，单次相机采集、局域网与云端多消费者）
  → ffmpeg（H.264 + yuv420p，MPEG-TS 写 stdout）
  → 进程内 libsrt 发送（streamid / passphrase 只在进程内存中，绝不进入任何 argv / 日志）
  → 云端 MediaMTX（per-vehicle per-camera 独立发布路径 <vehicle_id>-<camera>）。

运行环境：Ubuntu 20.04（python3.8 / FFmpeg 4.2.x / libsrt 1.4.x）。本包只用标准库 + ctypes。
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
