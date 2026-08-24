"""车端 Bridge 身份：boot_id 生命周期与 client_id。

boot_id 规则：
  - Bridge 进程启动时生成新 uuid（boot 会话）
  - MQTT 断开重连：boot_id 不变（重连 ≠ 进程重启）
  - 进程重启：新 boot_id
服务器 RobotBootSession 按此管理。
"""
from __future__ import annotations

import uuid


class Identity:
    def __init__(self, vehicle_id: str) -> None:
        self.vehicle_id = vehicle_id
        self.boot_id: str = str(uuid.uuid4())
        self._client_id: str | None = None

    @property
    def client_id(self) -> str:
        """同 boot 内稳定的 MQTT client id（重连不换）。"""
        if self._client_id is None:
            self._client_id = f"firebot-bridge-{self.vehicle_id}-{self.boot_id[:8]}"
        return self._client_id
