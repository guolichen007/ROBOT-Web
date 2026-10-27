#!/usr/bin/env python3
"""secret redaction 单测（P0-B）。

全部使用合成测试值（test-* / WRONG_*）；不得把任何真实 secret 写入本文件或快照。
断言重点：
  - passphrase/password/token/authorization/streamid credential 全部掩码
  - hostname/port/vehicle_id/stream path/错误类型保留
  - 掩码后文本不含原始 credential 值
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.redact_secrets import redact_secrets  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


FAKE_TOKEN = "test-token-abcd1234"
FAKE_PASSPHRASE = "test-passphrase-WxYz+123/456"
FAKE_MQTT_PW = "test-mqtt-password-xyz"


def test_srt_url() -> None:
    url = (
        f"srt://firebot.youdaozd.com:8890?streamid=publish:R001-roof_rgb:R001:{FAKE_TOKEN}"
        f"&pkt_size=1316&passphrase={FAKE_PASSPHRASE}"
    )
    out = redact_secrets(url)
    check("SRT URL passphrase 掩码", FAKE_PASSPHRASE not in out, out)
    check("SRT URL token 掩码", FAKE_TOKEN not in out, out)
    check("SRT URL hostname/port 保留", "firebot.youdaozd.com:8890" in out, out)
    check("SRT URL path/vehicle 保留", "R001-roof_rgb:R001:***" in out, out)
    check("SRT URL passphrase=***", "passphrase=***" in out, out)


def test_percent_encoded_passphrase() -> None:
    encoded = "test-passphrase-WxYz%2B123%2F456"
    out = redact_secrets(f"srt://h:8890?streamid=x&passphrase={encoded}")
    check("percent-encoded passphrase 掩码", encoded not in out and "passphrase=***" in out, out)


def test_rtsp_token() -> None:
    url = f"rtsp://mediamtx:8554/R001-roof_rgb?token={FAKE_TOKEN}"
    out = redact_secrets(url)
    check("RTSP token 掩码", FAKE_TOKEN not in out and "token=***" in out, out)
    check("RTSP host/port/path 保留", "mediamtx:8554/R001-roof_rgb" in out, out)


def test_streamid_in_log_lines() -> None:
    line = (
        "[out#0/mpegts] Error opening output "
        f"srt://firebot.youdaozd.com:8890?streamid=publish:R001-roof_thermal:R001:{FAKE_TOKEN}"
        f"&pkt_size=1316&passphrase={FAKE_PASSPHRASE}: I/O error"
    )
    out = redact_secrets(line)
    check(
        "libSRT 日志行 token/passphrase 掩码",
        FAKE_TOKEN not in out and FAKE_PASSPHRASE not in out,
        out,
    )
    check("错误类型保留", "I/O error" in out, out)
    check("thermal path 保留", "R001-roof_thermal:R001:***" in out, out)


def test_authorization_header() -> None:
    for text in (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefg",
        "authorization=bearer eyJhbGciOiJIUzI1NiJ9.abcdefg",
    ):
        out = redact_secrets(text)
        check("Authorization/Bearer 掩码", "eyJhbGciOiJIUzI1NiJ9.abcdefg" not in out, out)


def test_password_query() -> None:
    out = redact_secrets(f"?user=R001&password={FAKE_MQTT_PW}&x=1")
    check("password 查询掩码", FAKE_MQTT_PW not in out and "password=***" in out, out)


def test_idempotent_and_non_string() -> None:
    url = f"srt://h:8890?streamid=publish:R001-roof_rgb:R001:{FAKE_TOKEN}&passphrase={FAKE_PASSPHRASE}"
    once = redact_secrets(url)
    check("redact 幂等", redact_secrets(once) == once, once)
    check("非字符串原样返回", redact_secrets(None) is None)


def main() -> int:
    test_srt_url()
    test_percent_encoded_passphrase()
    test_rtsp_token()
    test_streamid_in_log_lines()
    test_authorization_header()
    test_password_query()
    test_idempotent_and_non_string()
    if FAILURES:
        print(f"\nSECRET_REDACTION_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nSECRET_REDACTION_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
