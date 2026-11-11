"""服务实例身份与隔离辅助（G1-P2：双 Adapter/Ingress 共存共享 PG/Redis）。

- instance_id：FIREBOT_SERVICE_INSTANCE_ID 或主机名兜底，写入者与 healthcheck 必须
  使用同一推导（本模块是唯一实现），否则心跳键错位会造成假阴性。
- heartbeat_key：心跳键按实例命名，专属实例不再覆盖原服务心跳。
- v1_scope：专属实例只处理白名单 vehicle_id 的消息（空=全部），防止独立 Broker 上
  非目标车辆流量落入共享 PG/Redis。
"""

from __future__ import annotations

import os
import socket

from app.core.config import get_settings


def instance_id() -> str:
    return os.environ.get("FIREBOT_SERVICE_INSTANCE_ID") or socket.gethostname()


def heartbeat_key(service: str, instance: str | None = None) -> str:
    return f"service:{service}:heartbeat:{instance or instance_id()}"


def v1_scope_set() -> set[str] | None:
    """None 表示不限范围（原有行为）；否则为专属实例的 vehicle_id 白名单。"""
    raw = get_settings().vehicle_agent_scope_vehicle_ids.strip()
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def in_v1_scope(vehicle_id: str) -> bool:
    scope = v1_scope_set()
    return scope is None or vehicle_id in scope
