"""capabilities 消息（能力声明，QoS1 retain）。

supported_commands 只声明真实可接受执行的能力；占位接口不宣称 supported。
"""
from __future__ import annotations

from ..config import Config
from ..protocol import Protocol


def make_capabilities(proto: Protocol, config: Config) -> dict:
    msg = proto.base("capabilities")
    msg.update(
        {
            "protocol_version": config.protocol_version,
            "supported_commands": list(config.supported_commands),
            "sensors": list(config.sensors),
            "media": list(config.media),
        }
    )
    return msg
