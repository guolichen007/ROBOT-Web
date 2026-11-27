"""WEB-R1B OperationReadiness (contract 2.1.0-draft.3) — the single authority for the console.

Built on top of the legacy robot_readiness() (kept as a compatibility projection), and mirrors
the final publish gate's freeze / environment / allowlist decisions, so the console can never
show an action as available that the gate would refuse. Pure function of server facts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.core.config import is_synthetic_namespace

CONTRACT_VERSION = "2.1.0-draft.3"
VALIDITY = timedelta(seconds=2)
# contract action -> (legacy readiness getter, permission)
ACTIONS: dict[str, tuple[tuple[str, str | None], str]] = {
    "stop_motion": (("safety_command_ready", "stop_motion"), "robot.control.stop"),
    "emergency_stop": (("safety_command_ready", "emergency_stop"), "robot.control.estop"),
    "reset_estop": (("safety_command_ready", "reset_estop"), "robot.control.reset_estop"),
    "patrol_start": (("autonomous_task_ready", "patrol"), "patrol.create"),
    "return_home": (("autonomous_task_ready", "return_dock"), "patrol.create"),
    "manual_control": (("manual_control_ready", None), "robot.control.manual"),
    # not implemented server-side yet: always unavailable, never faked
    "protective_stop": (("", None), "robot.control.stop"),
}
NOT_IMPLEMENTED = {"protective_stop": "PROTECTIVE_STOP_NOT_IMPLEMENTED"}
STOP_CLASS = {"stop_motion", "protective_stop"}


def _legacy(readiness: dict[str, Any], key: tuple[str, str | None]) -> bool:
    group, name = key
    if not group:
        return False
    value = readiness.get(group)
    return bool(value.get(name)) if name and isinstance(value, dict) else bool(value)


def freeze_state(robot, settings) -> tuple[bool, list[str], str | None]:
    """(frozen, reasons, scope) — same decisions as services/dispatch_gate.py."""
    reasons: list[str] = []
    synthetic = is_synthetic_namespace(robot.vehicle_id)
    env = settings.firebot_deployment_env
    if robot.agent_protocol == "VEHICLE_AGENT_V1":
        # 001 keeps its own independent V1 freeze (downlink flag + V1 allowlist)
        if not settings.vehicle_downlink_allowed(robot.vehicle_id):
            reasons.append("V1_DOWNLINK_DISABLED")
    else:
        if (env == "SIM") != synthetic:
            reasons.append("DEPLOYMENT_ENV_MISMATCH")
        if settings.downlink_frozen and not (env == "SIM" and settings.sim_downlink_enabled):
            reasons.append("DOWNLINK_FROZEN")
        if robot.vehicle_id not in settings.canonical_downlink_allowlist_set:
            reasons.append("NOT_ALLOWLISTED")
    if not synthetic and reasons:
        reasons.insert(0, "REAL_CONTROL_NOT_AUTHORIZED")
    frozen = bool(reasons)
    scope = None if not frozen else ("ALL" if not synthetic else "ALL_EXCEPT_STOP_MOTION")
    return frozen, reasons, scope


def operation_readiness(
    robot, readiness: dict[str, Any], *, settings, permissions: set[str], now: datetime
) -> dict[str, Any]:
    synthetic = is_synthetic_namespace(robot.vehicle_id)
    frozen, freeze_reasons, scope = freeze_state(robot, settings)
    exempt = STOP_CLASS if scope == "ALL_EXCEPT_STOP_MOTION" else set()
    online = robot.enabled and robot.online_state == "ONLINE"
    actions: dict[str, Any] = {}
    for name, (key, permission) in ACTIONS.items():
        reasons: list[str] = []
        if name in NOT_IMPLEMENTED:
            reasons.append(NOT_IMPLEMENTED[name])
        elif not _legacy(readiness, key):
            reasons.extend(readiness.get("readiness_reasons") or ["NOT_READY"])
        if not online:
            reasons.append(f"ROBOT_{robot.online_state or 'UNKNOWN'}")
        if robot.boot_id is None and name != "emergency_stop":
            reasons.append("ROBOT_BOOT_SESSION_UNKNOWN")
        if frozen and name not in exempt:
            reasons.append("CONTROL_FROZEN")
        permitted = permission in permissions
        if not permitted:
            reasons.append("PERMISSION_DENIED")
        reasons = list(dict.fromkeys(r for r in reasons if r))
        actions[name] = {
            "allowed": not reasons,
            "reasons": reasons,
            "required_permission": permission,
            "permitted_for_user": permitted and not reasons,
            "confirm": "HOLD" if name in {"emergency_stop", "reset_estop"} else "CLICK",
        }
    return {
        "contract_version": CONTRACT_VERSION,
        "kind": "OperationReadiness",
        "vehicle_id": robot.vehicle_id,
        "environment": "SIM" if synthetic else "REAL",
        "evaluated_at": now.isoformat(),
        "valid_until": (now + VALIDITY).isoformat(),
        "boot_id": robot.boot_id,
        "source": "SERVER_OPERATION_READINESS",
        "based_on_snapshot_at": now.isoformat(),
        "control_frozen": frozen,
        "freeze_scope": scope,
        "freeze_reasons": freeze_reasons,
        "control_owner": None,
        "actions": actions,
    }
