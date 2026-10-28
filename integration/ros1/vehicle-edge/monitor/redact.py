"""本地日志脱敏（firebot-edge logs 与 audit 输出用；复用脱敏规则语义）。

模式与 scripts/redact_secrets.py 保持一致：streamid publish:path:user:cred、
passphrase=/passwd=/password=/token= query values、Authorization/Bearer。
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(streamid=publish:[^:]+:[^:]+:)[^&\s\"']+"),
    re.compile(r"([?&](?:passphrase|passwd|password|token|secret|credential)=)[^&\s\"']+"),
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+"),
]


def redact(text: str) -> str:
    """对文本做 best-effort 脱敏（保持 hostname/port/vehicle_id/path/error type）。"""
    for pattern in _PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


def redact_lines(text: str) -> str:
    """按行脱敏（行缓冲，避免跨 chunk 边界泄漏）。"""
    return "\n".join(redact(line) for line in text.splitlines())
