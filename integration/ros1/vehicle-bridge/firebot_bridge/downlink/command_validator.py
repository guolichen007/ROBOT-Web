"""下行命令校验（协议/boot/过期/能力支持性/任务锁前置）。"""
from __future__ import annotations

from ..config import Config
from ..protocol import TASK_CMDS, validate_command


def validate_received_command(
    command: dict, boot_id: str, config: Config
) -> tuple[bool, str | None]:
    """返回 (ok, reason_code)。reason_code 为 schema 枚举白名单。"""
    ok, reason = validate_command(command, boot_id)
    if not ok:
        return False, reason
    cmd = command.get("cmd")
    # 任务类命令必须携带非空 task_id，否则内部任务锁形同虚设
    if cmd in TASK_CMDS and not command.get("task_id"):
        return False, "INVALID_PROTOCOL_MESSAGE"
    # 能力支持性：生产模式只接受声明支持的命令；stub 联调模式放行全部（用于消息联调）
    if not config.bridge_stub_mode and config.supported_commands and cmd not in config.supported_commands:
        return False, "COMMAND_UNSUPPORTED"
    return True, None
