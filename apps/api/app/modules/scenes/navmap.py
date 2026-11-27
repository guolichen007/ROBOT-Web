"""Server-side ROS map_server import validation (authoritative; the browser check is a preview).

Mirrors apps/web/src/lib/nav-map.ts rule-for-rule. Limits are the API's: the image uses the
existing upload limit (settings.upload_max_bytes = 10 MiB) until a real R002 map is evaluated.
Never resolves any path from the YAML: the YAML may only name the image uploaded with it.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass

YAML_MAX_BYTES = 64 * 1024
MAX_SIDE_PX = 8192
MAX_PIXELS = 32_000_000
ALLOWED_KEYS = {"image", "resolution", "origin", "negate", "occupied_thresh", "free_thresh", "mode"}
REQUIRED_KEYS = ("image", "resolution", "origin", "occupied_thresh", "free_thresh")
IMAGE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\.(pgm|png)$", re.IGNORECASE)
NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
ORIGIN = re.compile(r"^\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^,\]]+)\s*\]$")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
# magic, width, height, maxval separated by whitespace/comments; exactly one whitespace byte
# ends the header (Netpbm), so m.end() is the first raster byte.
_WS = rb"(?:\s|#[^\n]*\n)+"
PGM_HEADER = re.compile(
    rb"(P[25])" + _WS + rb"(\d{1,6})" + _WS + rb"(\d{1,6})" + _WS + rb"(\d{1,3})\s"
)


class NavMapError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class NavMapMeta:
    image: str
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    negate: int
    occupied_thresh: float
    free_thresh: float
    mode: str


@dataclass(frozen=True)
class Raster:
    fmt: str
    width: int
    height: int


def _finite(raw: str, key: str) -> float:
    if not NUMBER.fullmatch(raw):
        raise NavMapError("YAML_NOT_A_FINITE_NUMBER", key)
    value = float(raw)
    if not math.isfinite(value):
        raise NavMapError("YAML_NOT_A_FINITE_NUMBER", key)
    return value


def _unquote(raw: str) -> str:
    m = re.fullmatch(r"(['\"])(.*)\1", raw)
    return m.group(2) if m else raw


def parse_map_yaml(data: bytes) -> NavMapMeta:
    if len(data) > YAML_MAX_BYTES:
        raise NavMapError("YAML_TOO_LARGE", str(len(data)))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NavMapError("YAML_NOT_UTF8") from exc
    seen: dict[str, str] = {}
    for index, line in enumerate(text.lstrip("﻿").splitlines()):
        stripped = re.sub(r"\s+#.*$", "", line)
        stripped = re.sub(r"^#.*$", "", stripped).rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() == "---" and index == 0:
            continue
        if stripped[:1].isspace():
            raise NavMapError("YAML_NESTING_FORBIDDEN", f"line {index + 1}")
        if re.search(r"[&*!|>{}]", stripped) or stripped.startswith(("---", "...")):
            raise NavMapError("YAML_FEATURE_FORBIDDEN", f"line {index + 1}")
        m = re.fullmatch(r"([A-Za-z_]+):\s*(.*)", stripped)
        if not m:
            raise NavMapError("YAML_SYNTAX", f"line {index + 1}")
        key, value = m.group(1), m.group(2).strip()
        if key not in ALLOWED_KEYS:
            raise NavMapError("YAML_UNKNOWN_KEY", key)
        if key in seen:
            raise NavMapError("YAML_DUPLICATE_KEY", key)
        seen[key] = value
    for key in REQUIRED_KEYS:
        if key not in seen:
            raise NavMapError("YAML_MISSING_KEY", key)
    image = _unquote(seen["image"])
    if not IMAGE_NAME.fullmatch(image) or ".." in image:
        raise NavMapError("YAML_IMAGE_PATH_FORBIDDEN", image)
    resolution = _finite(seen["resolution"], "resolution")
    if not 0 < resolution <= 1:
        raise NavMapError("YAML_RESOLUTION_OUT_OF_RANGE", str(resolution))
    om = ORIGIN.fullmatch(seen["origin"])
    if not om:
        raise NavMapError("YAML_ORIGIN_SHAPE", seen["origin"])
    ox, oy, oyaw = (_finite(om.group(i).strip(), f"origin[{i - 1}]") for i in (1, 2, 3))
    if abs(ox) > 100_000 or abs(oy) > 100_000:
        raise NavMapError("YAML_ORIGIN_OUT_OF_RANGE")
    if abs(oyaw) > math.pi + 1e-9:
        raise NavMapError("YAML_ORIGIN_YAW_OUT_OF_RANGE", str(oyaw))
    occupied = _finite(seen["occupied_thresh"], "occupied_thresh")
    free = _finite(seen["free_thresh"], "free_thresh")
    if not (0 <= occupied <= 1 and 0 <= free <= 1 and free < occupied):
        raise NavMapError("YAML_THRESHOLDS_INVALID", f"free={free} occupied={occupied}")
    negate_raw = seen.get("negate", "0")
    if negate_raw not in ("0", "1"):
        raise NavMapError("YAML_NEGATE_INVALID", negate_raw)
    mode = _unquote(seen.get("mode", "trinary"))
    if mode not in ("trinary", "scale", "raw"):
        raise NavMapError("YAML_MODE_INVALID", mode)
    return NavMapMeta(image, resolution, ox, oy, oyaw, int(negate_raw), occupied, free, mode)


def _check_dims(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise NavMapError("IMAGE_DIMENSIONS_INVALID", f"{width}x{height}")
    if width > MAX_SIDE_PX or height > MAX_SIDE_PX or width * height > MAX_PIXELS:
        raise NavMapError("IMAGE_TOO_MANY_PIXELS", f"{width}x{height}")


# WEB-R1C R1C-3: the header is not the image. Every raster is FULLY decoded with resource limits
# (pixel budget checked before any allocation, Pillow decompression-bomb guard, bounded ASCII
# token count) and checked against ROS map_server semantics: all pixels present, none above the
# declared maxval, nothing trailing, 8-bit grey (or grey+alpha / RGB that map_server averages).
PGM_ASCII_MAX_BYTES = 64 * 1024 * 1024
PNG_ALLOWED_MODES = {"L", "LA", "RGB", "RGBA", "P", "1", "I;16", "I"}


def _parse_pgm(data: bytes) -> Raster:
    m = PGM_HEADER.match(data)
    if not m:
        raise NavMapError("PGM_MAGIC_UNSUPPORTED", data[:2].decode("ascii", "replace"))
    magic = m.group(1)
    width, height, maxval = (int(m.group(i)) for i in (2, 3, 4))
    _check_dims(width, height)
    if not 1 <= maxval <= 255:
        raise NavMapError("PGM_MAXVAL_UNSUPPORTED", str(maxval))
    count = width * height
    body = data[m.end() :]
    if magic == b"P5":
        if len(body) < count:
            raise NavMapError("PGM_DATA_TRUNCATED", f"{len(body)}<{count}")
        if len(body) > count:
            raise NavMapError("PGM_TRAILING_DATA", f"{len(body) - count} bytes")
        if max(body) > maxval:  # bytes are 0..255: one pass over the raster
            index = next(i for i, v in enumerate(body) if v > maxval)
            raise NavMapError("PGM_PIXEL_OVER_MAXVAL", f"pixel {index}={body[index]}>{maxval}")
        return Raster("pgm", width, height)
    # P2 (ASCII): comments allowed, exactly width*height decimal tokens, each <= maxval
    if len(body) > PGM_ASCII_MAX_BYTES:
        raise NavMapError("IMAGE_TOO_LARGE", str(len(body)))
    text = re.sub(rb"#[^\n]*", b" ", body)
    tokens = text.split()
    if len(tokens) < count:
        raise NavMapError("PGM_DATA_TRUNCATED", f"{len(tokens)}<{count} pixels")
    if len(tokens) > count:
        raise NavMapError("PGM_TRAILING_DATA", f"{len(tokens) - count} extra tokens")
    for index, token in enumerate(tokens):
        if not token.isdigit() or len(token) > 3:
            raise NavMapError("PGM_PIXEL_INVALID", f"pixel {index}")
        if int(token) > maxval:
            raise NavMapError("PGM_PIXEL_OVER_MAXVAL", f"pixel {index}={int(token)}>{maxval}")
    return Raster("pgm", width, height)


def _parse_png(data: bytes) -> Raster:
    import io
    import warnings

    from PIL import Image

    if len(data) < 33 or data[:8] != PNG_SIG or data[12:16] != b"IHDR":
        raise NavMapError("PNG_SIGNATURE_INVALID")
    width, height = struct.unpack(">II", data[16:24])
    _check_dims(width, height)  # before Pillow allocates anything
    if b"IEND" not in data[-12:]:
        raise NavMapError("PNG_TRUNCATED", "IEND chunk missing")
    previous = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=["PNG"]) as image:
                if image.size != (width, height):
                    raise NavMapError("PNG_HEADER_MISMATCH", f"{image.size}")
                if image.mode not in PNG_ALLOWED_MODES:
                    raise NavMapError("PNG_MODE_UNSUPPORTED", image.mode)
                if getattr(image, "is_animated", False):
                    raise NavMapError("PNG_ANIMATED_UNSUPPORTED")
                image.load()  # full decode: every IDAT byte, CRCs, zlib stream
    except NavMapError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise NavMapError("IMAGE_TOO_MANY_PIXELS", type(exc).__name__) from exc
    except (OSError, SyntaxError, ValueError, struct.error, EOFError) as exc:
        raise NavMapError("PNG_DECODE_FAILED", type(exc).__name__) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    return Raster("png", width, height)


def parse_raster(filename: str, data: bytes, max_bytes: int) -> Raster:
    if len(data) > max_bytes:
        raise NavMapError("IMAGE_TOO_LARGE", str(len(data)))
    lower = filename.lower()
    if lower.endswith(".png"):
        return _parse_png(data)
    if lower.endswith(".pgm"):
        return _parse_pgm(data)
    raise NavMapError("IMAGE_FORMAT_UNSUPPORTED", filename)


def bind_image(meta: NavMapMeta, uploaded_name: str) -> None:
    base = re.split(r"[\\/]", uploaded_name)[-1]
    if base != meta.image:
        raise NavMapError("YAML_IMAGE_NAME_MISMATCH", f"{meta.image} != {base}")
