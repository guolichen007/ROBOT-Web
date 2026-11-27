"""Generate Firebot Web V2.1 contract test vectors (deterministic).

usage: python packages/protocol-schemas/firebot-v2.1/build_vectors.py
Writes vectors/<Contract>.vectors.json = {"valid": [...], "invalid": [...]}.
Each invalid vector carries the expected ContractError.code.

Rules for samples:
- SIM samples use firebot-sim-r002-01 and a SYNTHETIC Gazebo scene (SIM_GAZEBO_R002).
  They are NOT R002 field coordinates and MUST NOT be used to build REAL tasks.
- REAL samples (firebot-vehicle-02) only show default-deny / unknown states;
  no REAL scene exists yet.
- DEMO_PARKING coordinates are deliberately not used anywhere.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from services.contracts_v21 import CONTRACT_VERSION, scene_digest  # noqa: E402

OUT = Path(__file__).resolve().parent / "vectors"
CV = CONTRACT_VERSION
SIM = "firebot-sim-r002-01"
REAL = "firebot-vehicle-02"
T0 = "2026-11-26T09:00:00+08:00"
T1 = "2026-11-26T09:00:05+08:00"
T2 = "2026-11-26T09:00:30+08:00"
BOOT = "6f1d2c3b-0a9e-4b8c-8d7e-1f2a3b4c5d6e"
BOOT_OLD = "0b0b0b0b-0a9e-4b8c-8d7e-1f2a3b4c5d6e"
OPER = "11111111-2222-4333-8444-555555555555"
SCENE = "a1000000-0000-4000-8000-000000000001"
P = [f"b1000000-0000-4000-8000-00000000000{i}" for i in range(1, 6)]
SLOT = "c1000000-0000-4000-8000-000000000001"
ROUTE = "d1000000-0000-4000-8000-000000000001"
CORR = "e1000000-0000-4000-8000-000000000001"
MISSION = "f1000000-0000-4000-8000-000000000001"
OP = "f2000000-0000-4000-8000-000000000001"
ALARM = "f3000000-0000-4000-8000-000000000001"
RECEIPT = "f4000000-0000-4000-8000-000000000001"


def pose(x: float, y: float, yaw: float) -> dict:
    return {"x": x, "y": y, "yaw": yaw, "frame_id": "map"}


def obs(value, quality="LIVE", at=T1, **extra) -> dict:
    return {
        "value": value,
        "quality": quality,
        "observed_at": at if quality in ("LIVE", "STALE") else None,
        **extra,
    }


def mutate(base: dict, path: list, value, delete: bool = False) -> dict:
    doc = copy.deepcopy(base)
    node = doc
    for key in path[:-1]:
        node = node[key]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


def inv(name: str, payload, code: str = "SCHEMA_INVALID") -> dict:
    return {"name": name, "payload": payload, "expect": code}


# ---------------------------------------------------------------- VehicleSnapshot
snap_sim = {
    "contract_version": CV,
    "kind": "VehicleSnapshot",
    "vehicle_id": SIM,
    "environment": "SIM",
    "generated_at": T1,
    "boot_id": BOOT,
    "link": {"state": "ONLINE", "last_seen_at": T1, "latency_ms": 42},
    "pose": {
        "quality": "LIVE",
        "observed_at": T1,
        "value": pose(3.2, -1.5, 1.5708),
        "localization": "OK",
        "map_code": "SIM_GAZEBO_R002",
        "map_version": "sim-1",
        "map_checksum": "sim-gazebo-r002-v1",
    },
    "battery_pct": obs(87.5),
    "smoke": obs(12.0, unit="UNVERIFIED", unit_verified=False, raw_source_field="sensor.smoke"),
    "temperature_c": obs(None, "UNSUPPORTED"),
    "estop": {"active": False, "quality": "LIVE", "observed_at": T1},
    "devices": [
        {
            "device": "LIDAR",
            "state": "OK",
            "quality": "LIVE",
            "observed_at": T1,
            "fault_code": None,
        },
        {
            "device": "FIRE_SHIELD",
            "state": "UNKNOWN",
            "quality": "UNKNOWN",
            "observed_at": None,
            "fault_code": None,
        },
    ],
    "active_mission_id": None,
    "scene": {
        "loaded_scene_id": SCENE,
        "loaded_scene_version": 1,
        "loaded_scene_digest": None,
        "load_state": "LOAD_PENDING",
    },
}
snap_real_unknown = {
    "contract_version": CV,
    "kind": "VehicleSnapshot",
    "vehicle_id": REAL,
    "environment": "REAL",
    "generated_at": T1,
    "boot_id": None,
    "link": {"state": "UNKNOWN", "last_seen_at": None, "latency_ms": None},
    "pose": {
        "quality": "UNKNOWN",
        "observed_at": None,
        "value": None,
        "localization": "UNKNOWN",
        "map_code": None,
        "map_version": None,
        "map_checksum": None,
    },
    "battery_pct": obs(None, "UNKNOWN"),
    "smoke": obs(None, "UNKNOWN", unit="UNVERIFIED", unit_verified=False),
    "temperature_c": obs(None, "UNSUPPORTED"),
    "estop": {"active": None, "quality": "UNKNOWN", "observed_at": None},
    "devices": [],
    "active_mission_id": None,
    "scene": {
        "loaded_scene_id": None,
        "loaded_scene_version": None,
        "loaded_scene_digest": None,
        "load_state": "NONE",
    },
}
snapshot_vectors = {
    "valid": [
        {"name": "sim_live", "payload": snap_sim},
        {"name": "real_unknown_never_zero", "payload": snap_real_unknown},
    ],
    "invalid": [
        inv("sim_env_with_real_vehicle_id", mutate(snap_sim, ["vehicle_id"], REAL)),
        inv("real_env_with_sim_vehicle_id", mutate(snap_real_unknown, ["vehicle_id"], SIM)),
        inv(
            "unknown_battery_filled_with_zero",
            mutate(snap_real_unknown, ["battery_pct", "value"], 0),
        ),
        inv("live_pose_without_value", mutate(snap_sim, ["pose", "value"], None)),
        inv("smoke_ppm_unverified", mutate(snap_sim, ["smoke", "unit"], "ppm")),
        inv("yaw_out_of_range", mutate(snap_sim, ["pose", "value", "yaw"], 7.0)),
        inv("frame_not_map", mutate(snap_sim, ["pose", "value", "frame_id"], "odom")),
        inv("battery_over_100", mutate(snap_sim, ["battery_pct", "value"], 101)),
        inv(
            "loaded_confirmed_without_digest",
            mutate(snap_sim, ["scene", "load_state"], "LOADED_CONFIRMED"),
        ),
        inv("estop_unknown_but_false", mutate(snap_real_unknown, ["estop", "active"], False)),
    ],
    "invalid_raw": [
        {
            "name": "nan_yaw_literal",
            "text": json.dumps(snap_sim).replace('"yaw": 1.5708', '"yaw": NaN'),
            "expect": "NON_FINITE_NUMBER",
        },
        {
            "name": "infinity_x_literal",
            "text": json.dumps(snap_sim).replace('"x": 3.2', '"x": Infinity'),
            "expect": "NON_FINITE_NUMBER",
        },
        {
            "name": "duplicate_key",
            "text": json.dumps(snap_sim)[:-1] + ', "vehicle_id": "x"}',
            "expect": "DUPLICATE_KEY",
        },
    ],
}


# ---------------------------------------------------------------- OperationReadiness
def act(allowed: bool, perm: str, reasons=None, confirm="CLICK") -> dict:
    return {
        "allowed": allowed,
        "reasons": [] if allowed else (reasons or ["NOT_READY"]),
        "required_permission": perm,
        "permitted_for_user": True if allowed else False,
        "confirm": confirm,
    }


ready_real_deny = {
    "contract_version": CV,
    "kind": "OperationReadiness",
    "vehicle_id": REAL,
    "environment": "REAL",
    "evaluated_at": T1,
    "valid_until": T2,
    "boot_id": None,
    "control_frozen": True,
    "freeze_reasons": ["REAL_CONTROL_NOT_AUTHORIZED", "ROBOT_BOOT_SESSION_UNKNOWN"],
    "control_owner": None,
    "actions": {
        "stop_motion": act(False, "robot.control.stop", ["ROBOT_OFFLINE"]),
        "protective_stop": act(False, "robot.control.stop", ["PROTECTIVE_STOP_NOT_IMPLEMENTED"]),
        "emergency_stop": act(False, "robot.control.estop", ["ROBOT_OFFLINE"], "HOLD"),
        "patrol_start": act(
            False, "patrol.create", ["REAL_CONTROL_NOT_AUTHORIZED", "SCENE_NOT_LOADED_ON_VEHICLE"]
        ),
        "navigate_to_point": act(False, "patrol.create", ["REAL_CONTROL_NOT_AUTHORIZED"]),
        "return_home": act(False, "patrol.create", ["REAL_CONTROL_NOT_AUTHORIZED"]),
        "manual_control": act(False, "robot.control.manual", ["REAL_CONTROL_NOT_AUTHORIZED"]),
        "fire_shield_open": act(
            False, "robot.control.device", ["DEVICE_FEEDBACK_CONTRACT_NOT_VERIFIED"]
        ),
    },
}
ready_sim = {
    "contract_version": CV,
    "kind": "OperationReadiness",
    "vehicle_id": SIM,
    "environment": "SIM",
    "evaluated_at": T1,
    "valid_until": T2,
    "boot_id": BOOT,
    "control_frozen": False,
    "freeze_reasons": [],
    "control_owner": {
        "user_id": OPER,
        "display_name": "SIM 操作员",
        "lease_id": OP,
        "expires_at": T2,
        "is_current_user": True,
    },
    "actions": {
        "stop_motion": act(True, "robot.control.stop"),
        "protective_stop": act(True, "robot.control.stop"),
        "emergency_stop": act(True, "robot.control.estop", confirm="HOLD"),
        "patrol_start": act(True, "patrol.create", confirm="DOUBLE"),
        "patrol_pause": act(False, "patrol.create", ["NO_ACTIVE_MISSION"]),
        "manual_control": act(False, "robot.control.manual", ["CONTROL_LEASE_REQUIRED"]),
    },
}
readiness_vectors = {
    "valid": [
        {"name": "real_default_deny", "payload": ready_real_deny},
        {"name": "sim_ready", "payload": ready_sim},
    ],
    "invalid": [
        inv(
            "allowed_with_reasons",
            mutate(ready_sim, ["actions", "patrol_start", "reasons"], ["X_Y"]),
        ),
        inv(
            "denied_without_reason",
            mutate(ready_real_deny, ["actions", "patrol_start", "reasons"], []),
        ),
        inv(
            "stop_entry_missing",
            mutate(ready_sim, ["actions", "protective_stop"], None, delete=True),
        ),
        inv(
            "unknown_action",
            mutate(ready_sim, ["actions", "drive_anywhere"], act(True, "robot.read")),
        ),
        inv("frozen_without_reason", mutate(ready_real_deny, ["freeze_reasons"], [])),
        inv(
            "frozen_but_patrol_allowed",
            mutate(ready_real_deny, ["actions", "patrol_start"], act(True, "patrol.create")),
            "FROZEN_BUT_ACTION_ALLOWED",
        ),
        inv(
            "boot_unknown_but_navigate_allowed",
            mutate(
                mutate(ready_sim, ["boot_id"], None),
                ["actions", "navigate_to_point"],
                act(True, "patrol.create"),
            ),
            "BOOT_UNKNOWN_BUT_ACTION_ALLOWED",
        ),
        inv(
            "valid_until_before_evaluated",
            mutate(ready_sim, ["valid_until"], T0),
            "READINESS_VALID_UNTIL_BEFORE_EVALUATED",
        ),
        inv(
            "allowed_but_user_lacks_permission",
            mutate(ready_sim, ["actions", "patrol_start", "permitted_for_user"], False),
        ),
    ],
}

# ---------------------------------------------------------------- SceneManifest (SIM synthetic)
nav = {
    "map_code": "SIM_GAZEBO_R002",
    "map_version": "sim-1",
    "frame_id": "map",
    "resolution_m_per_px": 0.05,
    "origin": pose(-10.0, -10.0, 0.0),
    "width_px": 400,
    "height_px": 400,
    "image_sha256": "1" * 64,
    "yaml_sha256": "2" * 64,
    "map_checksum": "sim-gazebo-r002-v1",
    "occupied_thresh": 0.65,
    "free_thresh": 0.196,
    "negate": 0,
}
points = [
    {
        "point_id": P[0],
        "code": "HOME",
        "kind": "RETURN_HOME",
        "pose": pose(0.0, 0.0, 0.0),
        "taught_from": "MANUAL_INPUT",
    },
    {
        "point_id": P[1],
        "code": "INSP-01",
        "kind": "INSPECTION",
        "pose": pose(4.0, 0.0, 0.0),
        "parking_slot_code": "S-01",
        "xy_tolerance_m": 0.2,
        "yaw_tolerance_rad": 0.15,
    },
    {"point_id": P[2], "code": "INSP-02", "kind": "INSPECTION", "pose": pose(8.0, 0.0, 0.0)},
    {
        "point_id": P[3],
        "code": "FIRE-01",
        "kind": "FIRE_OPERATION",
        "pose": pose(4.0, 1.0, 1.5708),
        "parking_slot_code": "S-01",
    },
]
scene_draft = {
    "contract_version": CV,
    "kind": "SceneManifest",
    "scene_id": SCENE,
    "scene_version": 1,
    "base_scene_version": None,
    "status": "DRAFT",
    "site_code": "SIM_GAZEBO",
    "nav_map": nav,
    "points": points,
    "parking_slots": [
        {
            "slot_id": SLOT,
            "code": "S-01",
            "center": pose(4.0, 2.5, 1.5708),
            "enabled": True,
            "polygon": [
                {"x": 2.8, "y": 1.5},
                {"x": 5.2, "y": 1.5},
                {"x": 5.2, "y": 3.5},
                {"x": 2.8, "y": 3.5},
            ],
        }
    ],
    "routes": [
        {
            "route_id": ROUTE,
            "code": "R-MAIN",
            "point_refs": [P[0], P[1], P[2]],
            "closed": False,
            "corridor_refs": [CORR],
        }
    ],
    "corridors": [
        {
            "corridor_id": CORR,
            "code": "LANE-1",
            "kind": "RIGHT_LANE",
            "centerline": [{"x": 0.0, "y": -0.5}, {"x": 8.0, "y": -0.5}],
            "width_m": 1.6,
        }
    ],
    "validation": {
        "schema_ok": True,
        "references_ok": True,
        "traversability": "PENDING_VERIFICATION",
        "traversability_evidence": None,
        "checked_at": T0,
        "issues": [],
    },
    "digest": None,
    "created_at": T0,
    "published_at": None,
    "published_by": None,
}
scene_pub = copy.deepcopy(scene_draft)
scene_pub.update({"status": "PUBLISHED", "published_at": T1, "published_by": OPER})
scene_pub["digest"] = scene_digest(scene_pub)


def republish(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["digest"] = scene_digest(doc)
    return doc


scene_vectors = {
    "valid": [
        {"name": "sim_draft", "payload": scene_draft},
        {"name": "sim_published_pending_traversability", "payload": scene_pub},
    ],
    "invalid": [
        inv("published_without_digest", mutate(scene_pub, ["digest"], None)),
        inv(
            "published_digest_tampered",
            mutate(scene_pub, ["points", 1, "pose", "x"], 4.5),
            "SCENE_DIGEST_MISMATCH",
        ),
        inv(
            "published_route_ref_missing",
            republish(mutate(scene_pub, ["routes", 0, "point_refs"], [P[0], P[4]])),
            "SCENE_REFERENCE_INVALID",
        ),
        inv(
            "published_dangling_corridor",
            republish(mutate(scene_pub, ["routes", 0, "corridor_refs"], [P[4]])),
            "SCENE_REFERENCE_INVALID",
        ),
        inv(
            "published_without_return_home",
            republish(
                mutate(
                    mutate(scene_pub, ["points"], points[1:]),
                    ["routes", 0, "point_refs"],
                    [P[1], P[2]],
                )
            ),
            "SCENE_REFERENCE_INVALID",
        ),
        inv(
            "published_traversability_failed",
            mutate(scene_pub, ["validation", "traversability"], "FAILED"),
        ),
        inv(
            "verified_without_evidence",
            mutate(scene_draft, ["validation", "traversability"], "VERIFIED"),
        ),
        inv("route_single_point", mutate(scene_draft, ["routes", 0, "point_refs"], [P[0]])),
        inv(
            "polygon_two_vertices",
            mutate(
                scene_draft, ["parking_slots", 0, "polygon"], [{"x": 0, "y": 0}, {"x": 1, "y": 1}]
            ),
        ),
        inv("resolution_zero", mutate(scene_draft, ["nav_map", "resolution_m_per_px"], 0)),
        inv("image_too_large", mutate(scene_draft, ["nav_map", "width_px"], 20000)),
        inv(
            "thresholds_inverted",
            mutate(scene_draft, ["nav_map", "free_thresh"], 0.9),
            "NAV_MAP_THRESHOLDS_INVALID",
        ),
        inv("point_coord_absurd", mutate(scene_draft, ["points", 1, "pose", "x"], 1e9)),
        inv("corridor_kind_left", mutate(scene_draft, ["corridors", 0, "kind"], "LEFT_LANE")),
        inv(
            "published_before_created",
            republish(mutate(scene_pub, ["published_at"], "2026-11-25T00:00:00+08:00")),
            "PUBLISHED_BEFORE_CREATED",
        ),
    ],
}

# ---------------------------------------------------------------- SceneLoadReceipt
receipt_ok = {
    "contract_version": CV,
    "kind": "SceneLoadReceipt",
    "receipt_id": RECEIPT,
    "vehicle_id": SIM,
    "environment": "SIM",
    "boot_id": BOOT,
    "seq": 10,
    "scene_id": SCENE,
    "scene_version": 1,
    "scene_digest": scene_pub["digest"],
    "nav_map_checksum": nav["map_checksum"],
    "result": "LOADED",
    "reason_code": None,
    "loaded_at": T1,
    "reported_at": T1,
}
receipt_rej = mutate(
    mutate(
        mutate(receipt_ok, ["result"], "REJECTED"), ["reason_code"], "NAV_MAP_CHECKSUM_MISMATCH"
    ),
    ["loaded_at"],
    None,
)
receipt_vectors = {
    "valid": [
        {"name": "sim_loaded", "payload": receipt_ok},
        {"name": "sim_rejected", "payload": receipt_rej},
    ],
    "invalid": [
        inv("loaded_with_reason", mutate(receipt_ok, ["reason_code"], "SOMETHING")),
        inv("rejected_without_reason", mutate(receipt_rej, ["reason_code"], None)),
        inv("loaded_without_loaded_at", mutate(receipt_ok, ["loaded_at"], None, delete=True)),
        inv("digest_not_hex", mutate(receipt_ok, ["scene_digest"], "Z" * 64)),
    ],
}

# ---------------------------------------------------------------- MissionRequest
mreq = {
    "contract_version": CV,
    "kind": "MissionRequest",
    "mission_id": MISSION,
    "idempotency_key": "web-v21-sim-0001",
    "vehicle_id": SIM,
    "environment": "SIM",
    "target_boot_id": BOOT,
    "mission_kind": "PATROL",
    "scene_id": SCENE,
    "scene_version": 1,
    "scene_digest": scene_pub["digest"],
    "route_snapshot": {
        "route_id": ROUTE,
        "start_index": 0,
        "waypoints": [
            {"index": 0, "point_id": P[0], "pose": points[0]["pose"]},
            {"index": 1, "point_id": P[1], "pose": points[1]["pose"]},
            {"index": 2, "point_id": P[2], "pose": points[2]["pose"]},
        ],
    },
    "resume_of": None,
    "issued_at": T0,
    "expires_at": T2,
    "operator_id": OPER,
}
mreq_nav = {k: v for k, v in mreq.items() if k != "route_snapshot"}
mreq_nav.update(
    {
        "mission_kind": "RETURN_HOME",
        "idempotency_key": "web-v21-sim-0002",
        "target": {"point_id": P[0], "pose": points[0]["pose"]},
    }
)
mission_vectors = {
    "valid": [
        {"name": "sim_patrol", "payload": mreq},
        {"name": "sim_return_home", "payload": mreq_nav},
    ],
    "invalid": [
        inv("patrol_without_route", mutate(mreq, ["route_snapshot"], None, delete=True)),
        inv("return_home_with_route", {**mreq_nav, "route_snapshot": mreq["route_snapshot"]}),
        inv("missing_target_boot", mutate(mreq, ["target_boot_id"], None, delete=True)),
        inv("real_vehicle_in_sim", mutate(mreq, ["vehicle_id"], REAL)),
        inv(
            "waypoint_index_gap",
            mutate(mreq, ["route_snapshot", "waypoints", 2, "index"], 5),
            "ROUTE_SNAPSHOT_INDEX_GAP",
        ),
        inv(
            "start_out_of_range",
            mutate(mreq, ["route_snapshot", "start_index"], 3),
            "ROUTE_SNAPSHOT_START_OUT_OF_RANGE",
        ),
        inv(
            "expires_before_issued",
            mutate(mreq, ["expires_at"], "2026-11-26T08:00:00+08:00"),
            "MISSION_EXPIRES_BEFORE_ISSUED",
        ),
        inv("pixel_pose_frame", mutate(mreq_nav, ["target", "pose", "frame_id"], "screen")),
        inv("short_idempotency_key", mutate(mreq, ["idempotency_key"], "k")),
    ],
}

# ---------------------------------------------------------------- MissionFeedback
fb = {
    "contract_version": CV,
    "kind": "MissionFeedback",
    "vehicle_id": SIM,
    "environment": "SIM",
    "boot_id": BOOT,
    "seq": 21,
    "mission_id": MISSION,
    "goal_id": "move_base-1732582800-7",
    "state": "EXECUTING",
    "phase": "NAVIGATING",
    "progress": 33.3,
    "waypoint_index": 1,
    "waypoint_total": 3,
    "current_point_id": P[1],
    "observed_at": T1,
}
fb_wait = {
    **fb,
    "seq": 22,
    "state": "WAITING_OBSTACLE",
    "phase": "OBSTACLE_WAIT",
    "obstacle_since": T1,
}
fb_done = {
    **fb,
    "seq": 30,
    "state": "SUCCEEDED",
    "phase": "DONE",
    "progress": 100,
    "final_pose": points[2]["pose"],
}
fb_fail = {
    **fb,
    "seq": 31,
    "state": "FAILED",
    "phase": "ABORTED",
    "failure_code": "NAV_GOAL_ABORTED",
    "failure_message": "planner aborted",
}
feedback_vectors = {
    "valid": [
        {"name": n, "payload": p}
        for n, p in [
            ("executing", fb),
            ("waiting_obstacle", fb_wait),
            ("succeeded", fb_done),
            ("failed", fb_fail),
        ]
    ],
    "invalid": [
        inv("failed_without_code", mutate(fb_fail, ["failure_code"], None, delete=True)),
        inv("obstacle_without_since", mutate(fb_wait, ["obstacle_since"], None, delete=True)),
        inv("succeeded_progress_not_100", mutate(fb_done, ["progress"], 90)),
        inv("succeeded_without_final_pose", mutate(fb_done, ["final_pose"], None, delete=True)),
        inv(
            "waypoint_out_of_range",
            mutate(fb, ["waypoint_index"], 3),
            "WAYPOINT_INDEX_OUT_OF_RANGE",
        ),
        inv("lowercase_state", mutate(fb, ["state"], "executing")),
    ],
    "stale_cases": [
        {
            "name": "old_boot",
            "task": {
                "mission_id": MISSION,
                "vehicle_id": SIM,
                "boot_id": BOOT,
                "goal_id": fb["goal_id"],
                "last_seq": 20,
                "state": "EXECUTING",
            },
            "feedback": {**fb, "boot_id": BOOT_OLD},
            "expect": "STALE_BOOT_ID",
        },
        {
            "name": "old_goal",
            "task": {
                "mission_id": MISSION,
                "vehicle_id": SIM,
                "boot_id": BOOT,
                "goal_id": "move_base-NEW",
                "last_seq": 20,
                "state": "EXECUTING",
            },
            "feedback": fb,
            "expect": "STALE_GOAL_ID",
        },
        {
            "name": "replayed_seq",
            "task": {
                "mission_id": MISSION,
                "vehicle_id": SIM,
                "boot_id": BOOT,
                "goal_id": fb["goal_id"],
                "last_seq": 21,
                "state": "EXECUTING",
            },
            "feedback": fb,
            "expect": "STALE_SEQ",
        },
        {
            "name": "after_terminal",
            "task": {
                "mission_id": MISSION,
                "vehicle_id": SIM,
                "boot_id": BOOT,
                "goal_id": fb["goal_id"],
                "last_seq": 5,
                "state": "CANCELLED",
            },
            "feedback": fb,
            "expect": "TASK_ALREADY_TERMINAL",
        },
        {
            "name": "fresh_ok",
            "task": {
                "mission_id": MISSION,
                "vehicle_id": SIM,
                "boot_id": BOOT,
                "goal_id": fb["goal_id"],
                "last_seq": 20,
                "state": "EXECUTING",
            },
            "feedback": fb,
            "expect": "OK",
        },
    ],
}

# ---------------------------------------------------- DeviceOperation (fire shield, manual)
dreq = {
    "contract_version": CV,
    "kind": "DeviceOperation",
    "message": "REQUEST",
    "operation_id": OP,
    "vehicle_id": SIM,
    "environment": "SIM",
    "device": "FIRE_SHIELD",
    "action": "OPEN",
    "control_mode": "MANUAL",
    "target_boot_id": BOOT,
    "issued_at": T0,
    "expires_at": T1,
    "operator_id": OPER,
}
dres = {
    "contract_version": CV,
    "kind": "DeviceOperation",
    "message": "RESULT",
    "operation_id": OP,
    "vehicle_id": SIM,
    "environment": "SIM",
    "device": "FIRE_SHIELD",
    "action": "OPEN",
    "boot_id": BOOT,
    "seq": 40,
    "result": {
        "state": "COMPLETED",
        "position": "OPEN",
        "limit_open": True,
        "limit_closed": False,
        "observed_at": T1,
        "reason_code": None,
    },
}
device_vectors = {
    "valid": [
        {"name": "open_request", "payload": dreq},
        {"name": "open_completed_with_limit_switch", "payload": dres},
    ],
    "invalid": [
        inv("auto_mode_deferred", mutate(dreq, ["control_mode"], "AUTO")),
        inv("completed_without_limit_switch", mutate(dres, ["result", "limit_open"], None)),
        inv(
            "completed_but_position_unknown",
            mutate(mutate(dres, ["result", "position"], "UNKNOWN"), ["result", "limit_open"], None),
            "DEVICE_COMPLETED_WITHOUT_LIMIT_CONFIRMATION",
        ),
        inv(
            "failed_without_reason",
            mutate(
                mutate(dres, ["result", "state"], "FAILED"), ["result", "position"], "INTERMEDIATE"
            ),
        ),
        inv("request_carrying_result", {**dreq, "result": dres["result"]}),
        inv("unknown_device", mutate(dreq, ["device"], "NOZZLE")),
    ],
}

# ---------------------------------------------------------------- AlarmEvent
alarm_fire = {
    "contract_version": CV,
    "kind": "AlarmEvent",
    "alarm_id": ALARM,
    "vehicle_event_id": "evt-001",
    "vehicle_id": SIM,
    "environment": "SIM",
    "category": "FIRE",
    "severity": "CRITICAL",
    "state": "NEW",
    "source": "VEHICLE",
    "fire_type": "flame",
    "confidence": 0.92,
    "position": pose(4.0, 2.5, 0.0),
    "parking_slot_code": "S-01",
    "first_seen_at": T0,
    "last_seen_at": T1,
    "occurrence_count": 2,
    "handling": [],
}
alarm_batt = {
    **alarm_fire,
    "category": "BATTERY_LOW",
    "severity": "MEDIUM",
    "source": "PLATFORM_RULE",
    "fire_type": "unknown",
    "measurement": {
        "metric": "battery",
        "value": 48.0,
        "unit": "percent",
        "unit_verified": True,
        "threshold": 50.0,
        "observed_at": T1,
    },
}
alarm_smoke_unverified = {
    **alarm_fire,
    "category": "SMOKE_HIGH",
    "severity": "HIGH",
    "source": "VEHICLE",
    "fire_type": "smoke",
    "measurement": {
        "metric": "smoke",
        "value": 41.0,
        "unit": "UNVERIFIED",
        "unit_verified": False,
        "threshold": None,
        "observed_at": T1,
    },
}
alarm_acked = {
    **alarm_fire,
    "state": "ACKNOWLEDGED",
    "handling": [{"action": "ACKNOWLEDGE", "actor_id": OPER, "at": T1}],
}
alarm_vectors = {
    "valid": [
        {"name": n, "payload": p}
        for n, p in [
            ("fire_new", alarm_fire),
            ("battery_low_50", alarm_batt),
            ("smoke_raw_unverified", alarm_smoke_unverified),
            ("fire_acked", alarm_acked),
        ]
    ],
    "invalid": [
        inv("fake_ppm", mutate(alarm_smoke_unverified, ["measurement", "unit"], "ppm")),
        inv(
            "unverified_with_threshold",
            mutate(alarm_smoke_unverified, ["measurement", "threshold"], 300),
        ),
        inv("battery_in_ppm", mutate(alarm_batt, ["measurement", "unit"], "ppm")),
        inv(
            "acked_without_record",
            mutate(alarm_acked, ["handling"], []),
            "ALARM_STATE_WITHOUT_HANDLING_RECORD",
        ),
        inv(
            "last_seen_before_first",
            mutate(alarm_fire, ["last_seen_at"], "2026-11-25T00:00:00+08:00"),
            "ALARM_LAST_SEEN_BEFORE_FIRST",
        ),
        inv(
            "battery_low_without_measurement",
            mutate(alarm_batt, ["measurement"], None, delete=True),
        ),
        inv("zero_occurrence", mutate(alarm_fire, ["occurrence_count"], 0)),
    ],
}

# ---------------------------------------------------------------- NavigationDiagnostics
nd = {
    "contract_version": CV,
    "kind": "NavigationDiagnostics",
    "vehicle_id": SIM,
    "environment": "SIM",
    "boot_id": BOOT,
    "seq": 50,
    "observed_at": T1,
    "mission_id": MISSION,
    "goal_id": "move_base-1732582800-7",
    "planner_state": "BLOCKED",
    "localization": {"state": "OK", "xy_std_m": 0.05, "yaw_std_rad": 0.02},
    "obstacle": {"blocked": True, "nearest_m": 0.6, "since": T0},
    "distance_to_goal_m": 3.1,
    "recovery_count": 1,
    "cmd_vel_source": "NAV",
}
nd_idle = {
    **nd,
    "mission_id": None,
    "goal_id": None,
    "planner_state": "IDLE",
    "obstacle": {"blocked": None, "nearest_m": None, "since": None},
    "distance_to_goal_m": None,
    "recovery_count": 0,
}
nav_vectors = {
    "valid": [
        {"name": "blocked", "payload": nd},
        {"name": "idle_unknown_obstacle", "payload": nd_idle},
    ],
    "invalid": [
        inv("blocked_but_not_blocked", mutate(nd, ["obstacle", "blocked"], False)),
        inv("negative_distance", mutate(nd, ["distance_to_goal_m"], -1)),
        inv("missing_localization", mutate(nd, ["localization"], None, delete=True)),
    ],
}

ALL = {
    "VehicleSnapshot": snapshot_vectors,
    "OperationReadiness": readiness_vectors,
    "SceneManifest": scene_vectors,
    "SceneLoadReceipt": receipt_vectors,
    "MissionRequest": mission_vectors,
    "MissionFeedback": feedback_vectors,
    "DeviceOperation": device_vectors,
    "AlarmEvent": alarm_vectors,
    "NavigationDiagnostics": nav_vectors,
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, vec in ALL.items():
        (OUT / f"{name}.vectors.json").write_text(
            json.dumps(vec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print("written", len(ALL), "vector files ->", OUT)
