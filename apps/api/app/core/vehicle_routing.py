"""G2 逐车传输路由与执行档（API / Dispatcher / Adapter 共用的唯一实现）。

- transport_profile：车辆 → Broker 路由。未列出车辆走 ``default``（原 Broker，行为不变）；
  列出的 profile 必须在 TRANSPORT_PROFILE_ADAPTERS 中声明，否则视为未知并拒绝派发，
  绝不回退到原 Broker。
- execution_profile：车辆 → 允许的命令及执行模式（services.vehicle_agent_v1.EXECUTION_PROFILES）。
  未列出车辆返回 None：沿用原有控制门禁（REAL），与 G2 前完全一致（002 不变）。
  列出车辆只允许档内命令，档外命令一律拒绝（不会降级为 REAL）。
"""

from __future__ import annotations

import hashlib
import json

from app.core.config import get_settings
from services.vehicle_agent_v1 import EXECUTION_PROFILES

DEFAULT_PROFILE = "default"


class RoutingError(ValueError):
    """路由/执行档配置不允许此命令（fail-closed）。"""


def _pairs(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise RoutingError(f"malformed routing entry: {item!r}")
        if key in result and result[key] != value:
            raise RoutingError(f"conflicting routing entry for {key!r}")
        result[key] = value
    return result


def profile_adapters() -> dict[str, str]:
    return _pairs(get_settings().transport_profile_adapters)


def known_profiles() -> set[str]:
    return {DEFAULT_PROFILE, *profile_adapters()}


def transport_profile(vehicle_id: str) -> str:
    """返回车辆的 transport_profile；未知 profile 抛 RoutingError（不回退）。"""
    profile = _pairs(get_settings().vehicle_transport_profiles).get(vehicle_id, DEFAULT_PROFILE)
    if profile not in known_profiles():
        raise RoutingError(f"unknown transport_profile {profile!r} for {vehicle_id}")
    return profile


def adapter_instance(profile: str) -> str | None:
    """None = 默认 profile（读兼容心跳键）；否则为专属 Adapter 实例 ID。"""
    if profile == DEFAULT_PROFILE:
        return None
    try:
        return profile_adapters()[profile]
    except KeyError as exc:
        raise RoutingError(f"unknown transport_profile {profile!r}") from exc


def execution_profile(vehicle_id: str) -> str | None:
    name = _pairs(get_settings().vehicle_execution_profiles).get(vehicle_id)
    if name is not None and name not in EXECUTION_PROFILES:
        raise RoutingError(f"unknown execution_profile {name!r} for {vehicle_id}")
    return name


def execution_mode(vehicle_id: str, action: str) -> str:
    """命令的执行模式：DIAGNOSTIC / SOFTWARE_TEST / REAL；档外命令抛 RoutingError。"""
    name = execution_profile(vehicle_id)
    if name is None:
        return "REAL"
    mode = EXECUTION_PROFILES[name].get(action)
    if mode is None:
        raise RoutingError(f"{action} not permitted by execution_profile {name!r}")
    return mode


def granted_commands(vehicle_id: str) -> list[str]:
    name = execution_profile(vehicle_id)
    return [] if name is None else list(EXECUTION_PROFILES[name])


def validate_configuration() -> None:
    """启动自检：任何畸形/未知 profile 让服务拒绝启动（而非运行期静默回退）。"""
    settings = get_settings()
    for vehicle in _pairs(settings.vehicle_transport_profiles):
        transport_profile(vehicle)
    for vehicle in _pairs(settings.vehicle_execution_profiles):
        execution_profile(vehicle)
    if settings.vehicle_agent_transport_profile not in known_profiles():
        raise RoutingError(
            f"instance transport_profile {settings.vehicle_agent_transport_profile!r} unknown"
        )


def config_fingerprint() -> str:
    """三侧（API / Dispatcher / Adapter）下行配置指纹：任一侧不一致即拒绝派发。"""
    settings = get_settings()
    material = {
        "downlink": settings.vehicle_agent_v1_downlink_enabled,
        "allowlist": sorted(settings.vehicle_downlink_allowlist_set),
        "transport": _pairs(settings.vehicle_transport_profiles),
        "adapters": profile_adapters(),
        "execution": _pairs(settings.vehicle_execution_profiles),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def fingerprint_key(instance: str) -> str:
    return f"service:vehicle-agent-adapter:routing:{instance}"


def adapter_config_consistent(redis, instance: str | None) -> bool:
    """非默认 profile：专属 Adapter 上报的配置指纹必须与本侧一致（fail-closed）。"""
    if instance is None:
        return True
    try:
        return redis.get(fingerprint_key(instance)) == config_fingerprint()
    except Exception:  # noqa: BLE001 - Redis 不可用即视为不一致
        return False
