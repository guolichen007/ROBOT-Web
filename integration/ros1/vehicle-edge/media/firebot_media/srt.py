"""进程内 libsrt 发送端（ctypes；libsrt 1.4.x ABI）。

为什么不用 srt-live-transmit / ffmpeg srt://：
  - Ubuntu 20.04 的 FFmpeg 4.2.7 未编译 libsrt（无 srt 协议）；
  - srt-live-transmit 1.4.0 只能经 URL 参数传 passphrase/streamid（会进入 argv），
    且不支持 SRT_ENV_* 环境变量（草稿 1c4bf74 依赖的变量实际从未生效）。
因此 streamid / passphrase 只经 srt_setsockflag 写入 socket，全程不出现在任何命令行。

错误分类：
  AUTH  — 对端拒绝（streamid 鉴权失败 / passphrase 错误）：长退避，不刷连接
  NET   — 超时 / 断链 / 解析失败：指数退避重连（长时间断网可无限自愈）
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import socket
import struct
from typing import Optional

SRTO_SNDSYN = 1
SRTO_SNDTIMEO = 13
SRTO_SENDER = 21
SRTO_LATENCY = 23
SRTO_PASSPHRASE = 26
SRTO_PBKEYLEN = 27
SRTO_CONNTIMEO = 36
SRTO_STREAMID = 46
SRTO_PAYLOADSIZE = 49
SRTO_TRANSTYPE = 50
SRTO_PEERIDLETIMEO = 55  # 经 libsrt 1.4.0 srt.h 编译求值核对（46/49/50/55）
SRTT_LIVE = 0
SRTS_CONNECTED = 5
LIVE_PAYLOAD = 1316

# SRT_REJECT_REASON（1.4.0 srt.h）
REJ_PEER = 2
REJ_VERSION = 8
REJ_BADSECRET = 10
REJ_UNSECURE = 11
AUTH_REJECTS = {REJ_PEER, REJ_BADSECRET, REJ_UNSECURE}
# MediaMTX（datarhei/gosrt）要求对端 SRT ≥ 1.4.1，低于即 REJ_VERSION（握手 1008 → libsrt reject=8）。
# Ubuntu 20.04 发行版 libsrt1 / srt-tools 为 1.4.0：实测永远被拒。必须使用 ≥1.4.1 的 libsrt。
MIN_VERSION = (1, 4, 1)


class SrtError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "AUTH" | "NET" | "LIB"（LIB = 永久：库缺失/版本过低）


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_uint32),
        ("sin_zero", ctypes.c_char * 8),
    ]


_LIB = None


def load_lib(path: Optional[str] = None, edge_home: Optional[str] = None):
    """加载 libsrt：显式路径 > 本 release 自带（<release>/lib/libsrt.so.1.4）> Edge Home 当前 release > 系统库。"""
    global _LIB
    if _LIB is not None:
        return _LIB
    candidates = [path] if path else []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(here, "lib", "libsrt.so.1.4"))
    if edge_home:
        candidates.append(os.path.join(edge_home, "media", "current", "lib", "libsrt.so.1.4"))
    candidates += ["libsrt.so.1.4", "libsrt.so.1", "libsrt-gnutls.so.1.4", "libsrt-openssl.so.1.4"]
    found = ctypes.util.find_library("srt")
    if found:
        candidates.append(found)
    last = None
    for name in candidates:
        if not name or ("/" in name and not os.path.exists(name)):
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as exc:
            last = exc
    else:
        raise SrtError("LIB", "libsrt 不可用（安装 libsrt1 / libsrt1.4-*）: %s" % last)
    lib.srt_getlasterror_str.restype = ctypes.c_char_p
    lib.srt_create_socket.restype = ctypes.c_int
    lib.srt_sendmsg.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    if lib.srt_startup() < 0:
        raise SrtError("LIB", "srt_startup 失败")
    lib.srt_setloglevel(2)  # LOG_CRIT：libsrt 自身日志不刷屏（且不含 streamid）
    version = version_tuple(lib)
    if version < MIN_VERSION:
        raise SrtError(
            "LIB",
            "libsrt %s 过低（MediaMTX 要求 ≥ %s；Ubuntu 20.04 发行版 1.4.0 必被 REJ_VERSION 拒绝）"
            "——先执行 firebotctl vehicle media install 安装受控 libsrt 1.4.4"
            % (
                ".".join(map(str, version)) if version != (0, 0, 0) else "<1.4.1",
                ".".join(map(str, MIN_VERSION)),
            ),
        )
    _LIB = lib
    return lib


def version_tuple(lib) -> tuple:
    """srt_getversion 自 1.4.1 起才导出；缺失即视为 1.4.0 及以下。"""
    try:
        fn = lib.srt_getversion
    except AttributeError:
        return (0, 0, 0)
    fn.restype = ctypes.c_uint32
    v = fn()
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def library_version(lib) -> str:
    v = version_tuple(lib)
    return "%d.%d.%d" % v if v != (0, 0, 0) else "<1.4.1"


class SrtSender:
    """单连接 SRT caller（live 模式，MPEG-TS 1316 字节分片）。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        streamid: str,
        passphrase: str,
        latency_ms: int = 200,
        connect_timeout_ms: int = 4000,
        peer_idle_ms: int = 5000,
        lib_path: Optional[str] = None,
    ) -> None:
        self._lib = load_lib(lib_path)
        self._host = host
        self._port = port
        self._streamid = streamid.encode("utf-8")
        self._passphrase = passphrase.encode("utf-8")
        self._latency = latency_ms
        self._conn_timeout = connect_timeout_ms
        self._peer_idle = peer_idle_ms
        self._sock = -1
        self._pending = b""
        self.bytes_sent = 0

    # -- socket options --------------------------------------------------
    def _int_opt(self, opt: int, value: int) -> None:
        v = ctypes.c_int(value)
        if self._lib.srt_setsockflag(self._sock, opt, ctypes.byref(v), ctypes.sizeof(v)) < 0:
            raise SrtError("LIB", "setsockflag(%d) 失败" % opt)

    def _str_opt(self, opt: int, value: bytes) -> None:
        if self._lib.srt_setsockflag(self._sock, opt, ctypes.c_char_p(value), len(value)) < 0:
            # 不回显 value（可能是 streamid/passphrase）
            raise SrtError("LIB", "setsockflag(%d) 失败（值长度 %d）" % (opt, len(value)))

    def connect(self) -> None:
        self.close()
        try:
            infos = socket.getaddrinfo(self._host, self._port, socket.AF_INET, socket.SOCK_DGRAM)
        except socket.gaierror as exc:
            raise SrtError("NET", "DNS 解析失败: %s" % exc.strerror) from None
        addr = infos[0][4][0]
        self._sock = self._lib.srt_create_socket()
        if self._sock < 0:
            raise SrtError("LIB", "srt_create_socket 失败")
        self._int_opt(SRTO_TRANSTYPE, SRTT_LIVE)
        self._int_opt(SRTO_SENDER, 1)
        self._int_opt(SRTO_CONNTIMEO, self._conn_timeout)
        self._int_opt(SRTO_LATENCY, self._latency)
        self._int_opt(SRTO_PEERIDLETIMEO, self._peer_idle)
        self._int_opt(SRTO_SNDTIMEO, 3000)
        self._int_opt(SRTO_PAYLOADSIZE, LIVE_PAYLOAD)
        self._str_opt(SRTO_STREAMID, self._streamid)
        self._str_opt(SRTO_PASSPHRASE, self._passphrase)
        sa = _SockaddrIn(
            socket.AF_INET,
            socket.htons(self._port),
            struct.unpack("<I", socket.inet_aton(addr))[0],
            b"",
        )
        if self._lib.srt_connect(self._sock, ctypes.byref(sa), ctypes.sizeof(sa)) < 0:
            reason = self._lib.srt_getrejectreason(self._sock)
            err = (self._lib.srt_getlasterror_str() or b"").decode("utf-8", "replace")
            self.close()
            if reason == REJ_VERSION:
                raise SrtError("LIB", "SRT 对端拒绝版本（REJ_VERSION）：本机 libsrt 过低")
            # 1000+ = 应用层拒绝（SRT_REJC_PREDEFINED/USERDEFINED，如 MediaMTX 鉴权拒绝 1401/1403）
            if reason in AUTH_REJECTS or reason >= 1000:
                raise SrtError("AUTH", "SRT 对端拒绝（reject=%d：鉴权或加密口令不匹配）" % reason)
            raise SrtError("NET", "SRT 连接失败（reject=%d）: %s" % (reason, err))

    def send(self, data: bytes) -> None:
        """按 1316 字节（7×188 TS 包）分片发送；不足一片的尾部缓存到下次。"""
        if self._sock < 0:
            raise SrtError("NET", "未连接")
        buf = self._pending + data
        n = len(buf) - (len(buf) % LIVE_PAYLOAD)
        for off in range(0, n, LIVE_PAYLOAD):
            chunk = buf[off : off + LIVE_PAYLOAD]
            if self._lib.srt_sendmsg(self._sock, chunk, len(chunk), -1, 1) < 0:
                err = (self._lib.srt_getlasterror_str() or b"").decode("utf-8", "replace")
                self.close()
                raise SrtError("NET", "SRT 发送失败（链路断开）: %s" % err)
            self.bytes_sent += len(chunk)
        self._pending = buf[n:]

    def close(self) -> None:
        if self._sock >= 0:
            self._lib.srt_close(self._sock)
            self._sock = -1
        self._pending = b""
