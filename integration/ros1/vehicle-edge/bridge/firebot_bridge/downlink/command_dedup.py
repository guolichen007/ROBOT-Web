"""command_id 幂等去重（QoS1 重复投递重放旧结果，不重复发布 ROS command）。"""
from __future__ import annotations

from ..state import BridgeState


class CommandDedup:
    def __init__(self, state: BridgeState) -> None:
        self.state = state

    def lookup(self, command_id: str) -> dict | None:
        """返回已处理记录的 {command, ack, task_status, terminal}；未见过返回 None。"""
        return self.state.get_command_record(command_id)

    def register(self, command_id: str, command: dict) -> None:
        self.state.register_command(command_id, command)

    def remember_ack(self, command_id: str, ack: dict) -> None:
        self.state.remember_ack(command_id, ack)

    def remember_task_status(self, command_id: str, task_status: dict) -> None:
        self.state.remember_task_status(command_id, task_status)

    def mark_terminal(self, command_id: str) -> None:
        self.state.mark_terminal(command_id)
