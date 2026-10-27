#!/usr/bin/env python3
"""secret redaction（P0-B）：SRT/FFmpeg/GStreamer 调用与验收日志脱敏。

背景：公网验收期间 ffmpeg/libSRT 的 stderr 把完整 SRT URL（含 streamid 内的发布
token 与 passphrase 查询值）原样写进了 console/持久日志——`-loglevel fatal` 无法
抑制 SRT 库告警。所有合成媒体 wrapper/验收脚本在输出 stderr 之前必须经本模块
redact（scripts/test_secret_redaction.py 有单测；services/media-test-source 的
Node 侧等价实现在 services/media-test-source/lib/redact.mjs）。

规则（顺序敏感）：
  - SRT streamid：publish:<path>:<user>:<cred> → 保留 path/user，cred 掩码 ***
  - URL 查询：passphrase=/passwd=/password=/token= 的值 → ***
  - Authorization/Bearer 头 → ***
保留：hostname、port、vehicle_id、stream path、错误类型。

用法：
  from scripts.redact_secrets import redact_secrets
  print(redact_secrets(stderr_tail))
"""

import re

# streamid=publish:<path>:<user>:<cred>（SRT 发布凭据三部分：path 与 user 保留）
_STREAMID_CRED = re.compile(r"(publish:[^:\s&'\"()]+:[^:\s&'\"()]+:)([^&\s'\"]+)")
# URL 查询中的 passphrase/passwd/password/token 值（直到 &、空白或引号）
_QUERY_CRED = re.compile(r"(?i)((?:passphrase|passwd|password|token|pwd)=)([^&\s'\"]+)")
# Authorization: Bearer xxx / authorization=xxx / Bearer xxx
_BEARER_CRED = re.compile(
    r"(?i)((?:authorization|bearer)[\s:=]+(?:bearer[\s:=]+)?)([A-Za-z0-9._~+\/\-]{12,})"
)


def redact_secrets(text: str) -> str:
    """对日志/错误文本做 credential 脱敏；输入不是 str 时原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    redacted = _STREAMID_CRED.sub(r"\1***", text)
    redacted = _QUERY_CRED.sub(r"\1***", redacted)
    redacted = _BEARER_CRED.sub(r"\1***", redacted)
    return redacted


if __name__ == "__main__":
    import sys

    sys.stdout.write(redact_secrets(sys.stdin.read()))
