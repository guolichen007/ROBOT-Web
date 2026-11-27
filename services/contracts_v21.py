"""Firebot Web V2.1 contract helpers (DRAFT 2.1.0-draft.2, NOT FROZEN).

Pure functions only: no DB, no MQTT, no network. Safe to import from API, workers
and tests. Real wire protocol stays Canonical MQTT 1.3 until an independent review
freezes any extension (see docs/技术合同/web-v2.1/).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_VERSION = "2.1.0-draft.2"
CONTRACTS = (
    "VehicleSnapshot",
    "OperationReadiness",
    "SceneManifest",
    "SceneLoadRequest",
    "SceneLoadReceipt",
    "MissionRequest",
    "MissionFeedback",
    "DeviceOperation",
    "AlarmEvent",
    "NavigationDiagnostics",
)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "protocol-schemas"
    / "firebot-v2.1"
    / "firebot-web-v2.1.contracts.schema.json"
)
TERMINAL_MISSION_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"})
# Legal MissionFeedback transitions. Platform-side initial state is DISPATCHED.
# Repeating the same state (progress update) is legal; going back (e.g. EXECUTING->ACCEPTED) is not.
MISSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "DISPATCHED": frozenset({"ACCEPTED", "REJECTED"}),
    "ACCEPTED": frozenset(
        {"ACCEPTED", "EXECUTING", "WAITING_OBSTACLE", "PAUSED", "FAILED", "CANCELLED"}
    ),
    "EXECUTING": frozenset(
        {"EXECUTING", "WAITING_OBSTACLE", "PAUSED", "SUCCEEDED", "FAILED", "CANCELLED"}
    ),
    "WAITING_OBSTACLE": frozenset(
        {"WAITING_OBSTACLE", "EXECUTING", "PAUSED", "FAILED", "CANCELLED"}
    ),
    "PAUSED": frozenset({"PAUSED", "EXECUTING", "FAILED", "CANCELLED"}),
}
GOAL_REQUIRED_STATES = frozenset({"EXECUTING", "WAITING_OBSTACLE", "SUCCEEDED"})
FEEDBACK_MAX_AGE_S = 10.0
SNAPSHOT_MAX_AGE_S = 5.0
CLOCK_SKEW_S = 2.0
# Business thresholds (display/alarm rules). Smoke threshold only applies when unit verified as ppm.
BATTERY_LOW_PCT = 50.0
TEMPERATURE_HIGH_C = 80.0
SMOKE_HIGH_PPM = 300.0


class ContractError(ValueError):
    """Raised for any contract violation; .code is a stable machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _reject_constant(name: str) -> Any:
    raise ContractError("NON_FINITE_NUMBER", name)


def strict_loads(text: str | bytes) -> Any:
    """RFC 8259 strict JSON: NaN/Infinity literals rejected, duplicate keys rejected."""

    def no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ContractError("DUPLICATE_KEY", key)
            out[key] = value
        return out

    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=no_dupes)


def _assert_finite(node: Any, path: str = "$") -> None:
    if isinstance(node, float) and not math.isfinite(node):
        raise ContractError("NON_FINITE_NUMBER", path)
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_finite(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_finite(value, f"{path}[{index}]")


@lru_cache(maxsize=1)
def bundle() -> dict[str, Any]:
    return strict_loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@cache
def validator(contract: str) -> Draft202012Validator:
    if contract not in CONTRACTS:
        raise ContractError("UNKNOWN_CONTRACT", contract)
    root = dict(bundle())
    root["$ref"] = f"#/$defs/{contract}"
    Draft202012Validator.check_schema(root)
    return Draft202012Validator(root, format_checker=FormatChecker())


def validate(contract: str, payload: Any) -> None:
    """Schema + finiteness + cross-field semantic rules. Raises ContractError."""
    _assert_finite(payload)
    errors = sorted(validator(contract).iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "$"
        raise ContractError("SCHEMA_INVALID", f"{where}: {first.message[:300]}")
    semantic = SEMANTIC_RULES.get(contract)
    if semantic:
        semantic(payload)


# ---------------------------------------------------------------- semantic rules


def _ts_order(earlier: str | None, later: str | None, code: str) -> None:
    from datetime import datetime

    if earlier and later and datetime.fromisoformat(later) < datetime.fromisoformat(earlier):
        raise ContractError(code, f"{later} < {earlier}")


def scene_digest(manifest: dict[str, Any]) -> str:
    """Canonical digest: sorted keys, no whitespace, UTF-8, over the content subset."""
    subset = {
        key: manifest[key]
        for key in (
            "scene_id",
            "scene_version",
            "nav_map",
            "points",
            "parking_slots",
            "routes",
            "corridors",
        )
    }
    canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scene_reference_issues(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Referential integrity inside ONE scene version (no cross-version refs possible)."""
    issues: list[dict[str, str]] = []

    def dupes(items: list[dict[str, Any]], key: str, code: str) -> set[str]:
        seen: set[str] = set()
        for item in items:
            if item[key] in seen:
                issues.append({"code": code, "ref": str(item[key])})
            seen.add(item[key])
        return seen

    point_ids = dupes(manifest["points"], "point_id", "DUPLICATE_POINT_ID")
    dupes(manifest["points"], "code", "DUPLICATE_POINT_CODE")
    dupes(manifest["parking_slots"], "slot_id", "DUPLICATE_SLOT_ID")
    slot_codes = dupes(manifest["parking_slots"], "code", "DUPLICATE_SLOT_CODE")
    corridor_ids = dupes(manifest["corridors"], "corridor_id", "DUPLICATE_CORRIDOR_ID")
    dupes(manifest["routes"], "route_id", "DUPLICATE_ROUTE_ID")
    dupes(manifest["routes"], "code", "DUPLICATE_ROUTE_CODE")
    for point in manifest["points"]:
        slot = point.get("parking_slot_code")
        if slot and slot not in slot_codes:
            issues.append({"code": "POINT_SLOT_REF_MISSING", "ref": point["code"]})
    for route in manifest["routes"]:
        refs = route["point_refs"]
        for ref in refs:
            if ref not in point_ids:
                issues.append({"code": "ROUTE_POINT_REF_MISSING", "ref": f"{route['code']}:{ref}"})
        for a, b in zip(refs, refs[1:], strict=False):
            if a == b:
                issues.append(
                    {"code": "ROUTE_REPEATED_ADJACENT_POINT", "ref": f"{route['code']}:{a}"}
                )
        for ref in route["corridor_refs"]:
            if ref not in corridor_ids:
                issues.append(
                    {"code": "ROUTE_CORRIDOR_REF_MISSING", "ref": f"{route['code']}:{ref}"}
                )
    if (
        not any(p["kind"] == "RETURN_HOME" for p in manifest["points"])
        and manifest["status"] != "DRAFT"
    ):
        issues.append({"code": "RETURN_HOME_POINT_MISSING", "ref": "points"})
    for corridor in manifest["corridors"]:
        line = corridor["centerline"]
        for a, b in zip(line, line[1:], strict=False):
            if math.hypot(b["x"] - a["x"], b["y"] - a["y"]) < 1e-3:
                issues.append({"code": "CORRIDOR_ZERO_LENGTH_SEGMENT", "ref": corridor["code"]})
                break
    return issues


def referenced_point_ids(manifest: dict[str, Any]) -> set[str]:
    return {ref for route in manifest["routes"] for ref in route["point_refs"]}


def can_delete_point(manifest: dict[str, Any], point_id: str) -> None:
    """Deleting a point that a route references is refused (反例: 删除被引用点位拒绝)."""
    if manifest["status"] != "DRAFT":
        raise ContractError("SCENE_NOT_DRAFT", manifest["status"])
    if point_id in referenced_point_ids(manifest):
        raise ContractError("POINT_REFERENCED_BY_ROUTE", point_id)


def _scene_rules(manifest: dict[str, Any]) -> None:
    issues = scene_reference_issues(manifest)
    if manifest["status"] != "DRAFT":
        if issues:
            raise ContractError(
                "SCENE_REFERENCE_INVALID", issues[0]["code"] + ":" + issues[0]["ref"]
            )
        if manifest["digest"] != scene_digest(manifest):
            raise ContractError("SCENE_DIGEST_MISMATCH", "digest != canonical content digest")
        _ts_order(manifest["created_at"], manifest.get("published_at"), "PUBLISHED_BEFORE_CREATED")
    nav = manifest["nav_map"]
    if nav["free_thresh"] >= nav["occupied_thresh"]:
        raise ContractError("NAV_MAP_THRESHOLDS_INVALID", "free_thresh must be < occupied_thresh")


def _readiness_rules(r: dict[str, Any]) -> None:
    _ts_order(r["evaluated_at"], r["valid_until"], "READINESS_VALID_UNTIL_BEFORE_EVALUATED")
    if r["control_frozen"]:
        for name, action in r["actions"].items():
            if action["allowed"] and name not in {
                "stop_motion",
                "protective_stop",
                "emergency_stop",
            }:
                raise ContractError("FROZEN_BUT_ACTION_ALLOWED", name)
    if r["boot_id"] is None:
        for name, action in r["actions"].items():
            if action["allowed"] and name != "emergency_stop":
                raise ContractError("BOOT_UNKNOWN_BUT_ACTION_ALLOWED", name)


def _scene_load_request_rules(r: dict[str, Any]) -> None:
    _ts_order(r["issued_at"], r["expires_at"], "LOAD_EXPIRES_BEFORE_ISSUED")


def _mission_request_rules(m: dict[str, Any]) -> None:
    _ts_order(m["issued_at"], m["expires_at"], "MISSION_EXPIRES_BEFORE_ISSUED")
    if m.get("resume_of") and m["execution_gen"] < 2:
        raise ContractError("RESUME_REQUIRES_NEW_EXECUTION_GEN", str(m["execution_gen"]))
    snap = m.get("route_snapshot")
    if snap:
        indexes = [w["index"] for w in snap["waypoints"]]
        if indexes != list(range(len(indexes))):
            raise ContractError("ROUTE_SNAPSHOT_INDEX_GAP", str(indexes[:10]))
        if snap["start_index"] >= len(indexes):
            raise ContractError("ROUTE_SNAPSHOT_START_OUT_OF_RANGE", str(snap["start_index"]))


def _mission_feedback_rules(f: dict[str, Any]) -> None:
    total, index = f["waypoint_total"], f["waypoint_index"]
    if index >= total:
        raise ContractError("WAYPOINT_INDEX_OUT_OF_RANGE", f"{index}/{total}")


def _device_rules(d: dict[str, Any]) -> None:
    if d["message"] == "REQUEST":
        _ts_order(d["issued_at"], d["expires_at"], "DEVICE_OP_EXPIRES_BEFORE_ISSUED")
        return
    result = d["result"]
    if result["state"] == "COMPLETED":
        expected = {"OPEN": "OPEN", "CLOSE": "CLOSED"}.get(d["action"])
        if expected and result["position"] != expected:
            raise ContractError("DEVICE_COMPLETED_WITHOUT_LIMIT_CONFIRMATION", result["position"])


def _alarm_rules(a: dict[str, Any]) -> None:
    _ts_order(a["first_seen_at"], a["last_seen_at"], "ALARM_LAST_SEEN_BEFORE_FIRST")
    if a["state"] != "NEW" and not a["handling"]:
        raise ContractError("ALARM_STATE_WITHOUT_HANDLING_RECORD", a["state"])
    m = a.get("measurement")
    if m and m["unit"] == "ppm" and m["threshold"] is not None and m["threshold"] != SMOKE_HIGH_PPM:
        raise ContractError("SMOKE_THRESHOLD_MISMATCH", str(m["threshold"]))


SEMANTIC_RULES = {
    "SceneManifest": _scene_rules,
    "SceneLoadRequest": _scene_load_request_rules,
    "OperationReadiness": _readiness_rules,
    "MissionRequest": _mission_request_rules,
    "MissionFeedback": _mission_feedback_rules,
    "DeviceOperation": _device_rules,
    "AlarmEvent": _alarm_rules,
}


# ---------------------------------------------------------------- state-machine guards


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def apply_feedback(
    task: dict[str, Any],
    feedback: dict[str, Any],
    *,
    now: datetime,
    max_age_s: float = FEEDBACK_MAX_AGE_S,
) -> tuple[bool, str, dict[str, Any]]:
    """Pure reducer: (accepted, code, next_task). A rejected feedback returns the task unchanged.

    task keys: vehicle_id, environment, mission_id, command_id, execution_gen, boot_id,
               state (DISPATCHED|...), last_seq|None, waypoint_index|None, waypoint_total,
               goal_id|None (goal bound to the current waypoint)
    """
    f = feedback
    if f["vehicle_id"] != task["vehicle_id"]:
        return False, "VEHICLE_MISMATCH", task
    if f["environment"] != task["environment"]:
        return False, "ENVIRONMENT_MISMATCH", task
    if f["mission_id"] != task["mission_id"]:
        return False, "MISSION_MISMATCH", task
    if f["command_id"] != task["command_id"]:
        return False, "COMMAND_MISMATCH", task
    if f["boot_id"] != task["boot_id"]:
        return False, "STALE_BOOT_ID", task
    if f["execution_gen"] < task["execution_gen"]:
        return False, "STALE_EXECUTION_GEN", task
    if f["execution_gen"] > task["execution_gen"]:
        return False, "UNKNOWN_EXECUTION_GEN", task
    if task["state"] in TERMINAL_MISSION_STATES:
        return False, "TASK_ALREADY_TERMINAL", task
    observed = _ts(f["observed_at"])
    if (now - observed).total_seconds() > max_age_s:
        return False, "FEEDBACK_TOO_OLD", task
    if (observed - now).total_seconds() > CLOCK_SKEW_S:
        return False, "FEEDBACK_FROM_FUTURE", task
    if task.get("last_seq") is not None and f["seq"] <= task["last_seq"]:
        return False, "STALE_SEQ", task  # covers exact replay of an already-applied message
    if f["waypoint_total"] != task["waypoint_total"]:
        return False, "WAYPOINT_TOTAL_MISMATCH", task
    current_wp = task.get("waypoint_index")
    if current_wp is not None and f["waypoint_index"] < current_wp:
        return False, "WAYPOINT_REGRESSION", task
    same_wp = current_wp is not None and f["waypoint_index"] == current_wp
    bound = task.get("goal_id") if same_wp else None
    if bound is not None:
        if f.get("goal_id") is None:
            return False, "GOAL_REQUIRED", task
        if f["goal_id"] != bound:
            return False, "STALE_GOAL_ID", task
    if f["state"] in GOAL_REQUIRED_STATES and not f.get("goal_id"):
        return False, "GOAL_REQUIRED", task
    if f["state"] not in MISSION_TRANSITIONS.get(task["state"], frozenset()):
        return False, "ILLEGAL_TRANSITION", task
    nxt = dict(task)
    nxt.update(
        state=f["state"],
        last_seq=f["seq"],
        waypoint_index=f["waypoint_index"],
        goal_id=f.get("goal_id") or bound,
        progress=f["progress"],
    )
    return True, "OK", nxt


def feedback_applies(
    task: dict[str, Any], feedback: dict[str, Any], *, now: datetime
) -> tuple[bool, str]:
    ok, code, _ = apply_feedback(task, feedback, now=now)
    return ok, code


def scene_load_confirms(
    manifest: dict[str, Any],
    request: dict[str, Any],
    receipt: dict[str, Any],
    *,
    current_boot_id: str | None,
) -> tuple[bool, str]:
    """A scene becomes a vehicle's executable basis ONLY via a LOADED receipt that answers
    exactly THIS load operation (load_id) on THIS vehicle/environment/boot, inside its window."""
    if manifest["status"] != "PUBLISHED":
        return False, "SCENE_NOT_PUBLISHED"
    if (request["scene_id"], request["scene_version"], request["scene_digest"]) != (
        manifest["scene_id"],
        manifest["scene_version"],
        manifest["digest"],
    ) or request["nav_map_checksum"] != manifest["nav_map"]["map_checksum"]:
        return False, "REQUEST_SCENE_MISMATCH"
    if receipt["load_id"] != request["load_id"]:
        return False, "RECEIPT_LOAD_MISMATCH"
    if receipt["vehicle_id"] != request["vehicle_id"]:
        return False, "RECEIPT_VEHICLE_MISMATCH"
    if receipt["environment"] != request["environment"]:
        return False, "RECEIPT_ENVIRONMENT_MISMATCH"
    if receipt["result"] != "LOADED":
        return False, f"RECEIPT_{receipt['result']}"
    if (
        current_boot_id is None
        or receipt["boot_id"] != current_boot_id
        or request["target_boot_id"] != current_boot_id
    ):
        return False, "RECEIPT_STALE_BOOT_ID"
    if (receipt["scene_id"], receipt["scene_version"]) != (
        manifest["scene_id"],
        manifest["scene_version"],
    ):
        return False, "RECEIPT_SCENE_MISMATCH"
    if receipt["scene_digest"] != manifest["digest"]:
        return False, "RECEIPT_DIGEST_MISMATCH"
    if receipt["nav_map_checksum"] != manifest["nav_map"]["map_checksum"]:
        return False, "RECEIPT_NAV_MAP_MISMATCH"
    issued, expires = _ts(request["issued_at"]), _ts(request["expires_at"])
    for key in ("loaded_at", "reported_at"):
        at = _ts(receipt[key])
        if at < issued or at > expires:
            return False, "RECEIPT_OUTSIDE_LOAD_WINDOW"
    return True, "OK"


def mission_admissible(
    manifest: dict[str, Any] | None,
    snapshot: dict[str, Any],
    readiness: dict[str, Any],
    action: str,
    *,
    now: datetime,
    snapshot_max_age_s: float = SNAPSHOT_MAX_AGE_S,
) -> tuple[bool, str]:
    """Server-side admission for a mission. Map CRUD/publish/load is NOT motion authorization:
    the (fresh, unexpired) readiness action must independently be allowed."""
    if manifest is None:
        return False, "SCENE_MISSING"
    if manifest["status"] != "PUBLISHED":
        return False, "SCENE_NOT_ACTIVE"  # ARCHIVED / DRAFT can never back a new mission
    if now >= _ts(readiness["valid_until"]):
        return False, "READINESS_EXPIRED"
    if (_ts(readiness["evaluated_at"]) - now).total_seconds() > CLOCK_SKEW_S:
        return False, "READINESS_FROM_FUTURE"
    if (now - _ts(snapshot["generated_at"])).total_seconds() > snapshot_max_age_s:
        return False, "SNAPSHOT_STALE"
    if (
        readiness["vehicle_id"] != snapshot["vehicle_id"]
        or readiness["environment"] != snapshot["environment"]
        or readiness["boot_id"] != snapshot["boot_id"]
    ):
        return False, "READINESS_SNAPSHOT_MISMATCH"
    if snapshot["boot_id"] is None:
        return False, "BOOT_UNKNOWN"
    if readiness["control_frozen"]:
        return False, "CONTROL_FROZEN"
    scene = snapshot["scene"]
    if scene["load_state"] != "LOADED_CONFIRMED":
        return False, "SCENE_NOT_LOADED_ON_VEHICLE"
    if (scene["loaded_scene_id"], scene["loaded_scene_version"], scene["loaded_scene_digest"]) != (
        manifest["scene_id"],
        manifest["scene_version"],
        manifest["digest"],
    ):
        return False, "SCENE_LOADED_VERSION_MISMATCH"
    entry = readiness["actions"].get(action)
    if not entry or not entry["allowed"]:
        return False, "READINESS_DENIED"
    return True, "OK"


# ---------------------------------------------------------------- business thresholds


def threshold_alarms(snapshot: dict[str, Any]) -> list[str]:
    """Exact boundaries: battery < 50 %, temperature > 80 degC, smoke > 300 ppm (verified only).
    Only LIVE readings raise; UNKNOWN/STALE never fabricate an alarm."""
    out: list[str] = []
    battery = snapshot.get("battery_pct")
    if battery and battery["quality"] == "LIVE" and battery["value"] < BATTERY_LOW_PCT:
        out.append("BATTERY_LOW")
    temp = snapshot.get("temperature_c")
    if temp and temp["quality"] == "LIVE" and temp["value"] > TEMPERATURE_HIGH_C:
        out.append("TEMPERATURE_HIGH")
    smoke = snapshot.get("smoke")
    if (
        smoke
        and smoke["quality"] == "LIVE"
        and smoke["unit"] == "ppm"
        and smoke["unit_verified"] is True
        and smoke["value"] > SMOKE_HIGH_PPM
    ):
        out.append("SMOKE_HIGH")
    return out
