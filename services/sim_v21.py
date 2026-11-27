"""WEB-R1C R1C-4: isolated SIM ingress for the V2.1 contracts (SceneLoadReceipt, MissionFeedback).

Only the reviewed contract functions decide (services/contracts_v21.py): apply_scene_receipt,
apply_feedback, confirm_mission_success. This module only maps them onto persistence.

Isolation: topics live in `_sim/v21/{vehicle}/{kind}`; mqtt-ingress subscribes to them ONLY when
FIREBOT_DEPLOYMENT_ENV=SIM, and every handler re-checks SIM env + firebot-sim-* namespace +
environment=SIM in the payload. No platform->vehicle publish exists here (no new downlink).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings, is_synthetic_namespace
from app.core.events import get_redis
from app.db.models import (
    MapVersion,
    Robot,
    SceneLoadReceiptRow,
    SceneLoadRequest,
    Task,
    TaskEvent,
)
from sqlalchemy import select

from services import contracts_v21 as c

TOPIC_PREFIX = "_sim/v21/"
KINDS = ("mission_feedback", "scene_receipt")
SIM_MISSION_TYPE = "SIM_V21_MISSION"
UNCONFIRMED = "SUCCEEDED_UNCONFIRMED"


class SimIngressRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sim_enabled() -> bool:
    return get_settings().firebot_deployment_env == "SIM"


def _guard(robot: Robot | None, payload: dict[str, Any]) -> Robot:
    if not sim_enabled():
        raise SimIngressRefused("SIM_INGRESS_DISABLED")
    if robot is None:
        raise SimIngressRefused("UNKNOWN_VEHICLE")
    if not is_synthetic_namespace(robot.vehicle_id) or payload.get("environment") != "SIM":
        raise SimIngressRefused("ENV_MISMATCH")
    if payload.get("vehicle_id") != robot.vehicle_id:
        raise SimIngressRefused("VEHICLE_MISMATCH")
    return robot


def _count(code: str) -> None:
    get_redis().hincrby("metrics:sim_v21", code, 1)


# ---------------------------------------------------------------- scene load receipt
def _manifest(db, version: MapVersion) -> dict[str, Any]:
    from app.modules.scenes.router import _verified_asset_bytes

    doc = json.loads(_verified_asset_bytes(db, version))
    doc.pop("route_arrivals", None)
    return doc


def handle_scene_receipt(db, robot: Robot | None, receipt: dict[str, Any], now: datetime) -> str:
    robot = _guard(robot, receipt)
    c.validate("SceneLoadReceipt", receipt)
    latest = db.get(SceneLoadRequest, robot.latest_scene_load_id or "")
    if latest is None:
        return "RECEIPT_NO_LOAD_REQUEST"
    state = "PENDING" if latest.state == "PENDING" else latest.state
    vehicle_scene = {
        "current_boot_id": robot.boot_id,
        "latest_request": latest.payload_json,
        "state": state,
    }
    manifest = _manifest(db, db.get(MapVersion, latest.map_version_id))
    applied, code, nxt = c.apply_scene_receipt(vehicle_scene, manifest, receipt)
    if not applied:
        _count(code)
        return code  # stale / duplicate / mismatching receipts never overwrite anything
    db.add(
        SceneLoadReceiptRow(
            receipt_id=receipt["receipt_id"],
            load_id=receipt["load_id"],
            robot_id=robot.id,
            boot_id=receipt["boot_id"],
            seq=receipt["seq"],
            result=receipt["result"],
            reason_code=receipt.get("reason_code"),
            loaded_at=datetime.fromisoformat(receipt["loaded_at"])
            if receipt.get("loaded_at")
            else None,
            reported_at=datetime.fromisoformat(receipt["reported_at"]),
            received_at=now,
            confirm_code=code,
            payload_json=receipt,
        )
    )
    latest.state = nxt["state"]
    if nxt["state"] == "LOADED_CONFIRMED":
        robot.loaded_scene_version_id = latest.map_version_id
        robot.loaded_scene_digest = manifest["digest"]
        robot.loaded_scene_boot_id = robot.boot_id
    return code


def clear_loaded_scene(robot: Robot) -> None:
    """A reboot voids the vehicle's loaded scene (it must be re-loaded and re-confirmed)."""
    robot.loaded_scene_version_id = None
    robot.loaded_scene_digest = None
    robot.loaded_scene_boot_id = None


def scene_state(robot: Robot, db) -> dict[str, Any]:
    if robot.loaded_scene_version_id and robot.loaded_scene_boot_id == robot.boot_id:
        version = db.get(MapVersion, robot.loaded_scene_version_id)
        return {
            "loaded_scene_id": version.id,
            "loaded_scene_version": version.revision,
            "loaded_scene_digest": robot.loaded_scene_digest,
            "load_state": "LOADED_CONFIRMED",
        }
    pending = db.get(SceneLoadRequest, robot.latest_scene_load_id or "")
    if pending is not None and pending.state == "PENDING":
        return {
            "loaded_scene_id": None,
            "loaded_scene_version": None,
            "loaded_scene_digest": None,
            "load_state": "LOAD_PENDING",
        }
    return {
        "loaded_scene_id": None,
        "loaded_scene_version": None,
        "loaded_scene_digest": None,
        "load_state": "NONE",
    }


# ---------------------------------------------------------------- platform VehicleSnapshot
def vehicle_snapshot(db, robot: Robot, now: datetime) -> dict[str, Any]:
    """The platform's OWN view of the vehicle (Redis latest from ingress + DB)."""
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {}
    observed = latest.get("source_timestamp")
    live = robot.online_state == "ONLINE" and observed and latest.get("x") is not None
    pose_value = (
        {"x": latest["x"], "y": latest["y"], "yaw": latest["theta"], "frame_id": "map"}
        if live
        else None
    )
    linear, angular = latest.get("linear_speed"), latest.get("angular_speed")
    stationary = (
        abs(float(linear)) < 0.01 and abs(float(angular)) < 0.01
        if live and linear is not None and angular is not None
        else None
    )
    return {
        "contract_version": c.CONTRACT_VERSION,
        "kind": "VehicleSnapshot",
        "vehicle_id": robot.vehicle_id,
        "environment": "SIM" if is_synthetic_namespace(robot.vehicle_id) else "REAL",
        "generated_at": now.isoformat(),
        "boot_id": robot.boot_id,
        "link": {
            "state": robot.online_state
            if robot.online_state in ("ONLINE", "STALE", "OFFLINE")
            else "UNKNOWN",
            "last_seen_at": robot.last_seen_at.isoformat() if robot.last_seen_at else None,
            "latency_ms": None,
        },
        "pose": {
            "quality": "LIVE" if live else "UNKNOWN",
            "observed_at": observed if live else None,
            "value": pose_value,
            "localization": (latest.get("localization_status") if live else "UNKNOWN")
            if (latest.get("localization_status") in ("OK", "DEGRADED", "LOST", "UNKNOWN"))
            else "UNKNOWN",
            "map_code": latest.get("map_code") if live else None,
            "map_version": latest.get("map_version") if live else None,
            "map_checksum": latest.get("map_checksum") if live else None,
        },
        "battery_pct": {"value": None, "quality": "UNKNOWN", "observed_at": None},
        "estop": {"active": None, "quality": "UNKNOWN", "observed_at": None},
        "active_mission_id": None,
        "scene": scene_state(robot, db),
        "motion": {
            "stationary": stationary,
            "quality": "LIVE" if stationary is not None else "UNKNOWN",
            "observed_at": observed if stationary is not None else None,
        },
    }


# ---------------------------------------------------------------- mission feedback
STATUS = {
    "ACCEPTED": "ACCEPTED",
    "EXECUTING": "EXECUTING",
    "WAITING_OBSTACLE": "EXECUTING",
    "PAUSED": "EXECUTING",
    "SUCCEEDED": "EXECUTING",  # until the platform confirms it with its own evidence
    "FAILED": "FAILED",
    "REJECTED": "FAILED",
    "CANCELLED": "CANCELLED",
}


def _event(db, task: Task, payload: dict[str, Any]) -> None:
    db.add(
        TaskEvent(
            task_id=task.id,
            status=task.status,
            phase=task.phase,
            progress=task.progress,
            payload_json=payload,
        )
    )


def handle_mission_feedback(db, robot: Robot | None, fb: dict[str, Any], now: datetime) -> str:
    robot = _guard(robot, fb)
    c.validate("MissionFeedback", fb)
    task = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type == SIM_MISSION_TYPE,
            Task.parameters_json["mission_id"].as_string() == fb["mission_id"],
        )
        .with_for_update()
    )
    if task is None:
        _count("MISSION_UNKNOWN")
        return "MISSION_UNKNOWN"
    params = dict(task.parameters_json)
    ok, code, nxt = c.apply_feedback(params["v21"], fb, now=now)
    if not ok:
        _count(code)
        rejected = dict(params.get("rejected") or {})
        rejected[code] = rejected.get(code, 0) + 1
        params["rejected"] = rejected
        task.parameters_json = params
        return code
    params["v21"] = nxt
    task.status = STATUS[nxt["state"]]
    task.phase = nxt["state"] if nxt["state"] != "SUCCEEDED" else UNCONFIRMED
    task.progress = float(nxt.get("progress") or 0)
    if nxt["state"] == "EXECUTING" and task.started_at is None:
        task.started_at = now
    if nxt["state"] in ("FAILED", "REJECTED", "CANCELLED"):
        task.completed_at = now
        task.failure_code = fb.get("failure_code")
    task.parameters_json = params
    _event(db, task, {"feedback_seq": fb["seq"], "state": nxt["state"], "code": code})
    if nxt["state"] == "SUCCEEDED":
        confirm_pending(db, robot, now, task=task)
    return code


def confirm_pending(db, robot: Robot, now: datetime, task: Task | None = None) -> str | None:
    """Platform-side success confirmation; retried on each fresh location sample."""
    task = task or db.scalar(
        select(Task).where(
            Task.robot_id == robot.id,
            Task.type == SIM_MISSION_TYPE,
            Task.phase == UNCONFIRMED,
        )
    )
    if task is None:
        return None
    params = dict(task.parameters_json)
    ok, code = c.confirm_mission_success(
        params["request"], params["v21"], vehicle_snapshot(db, robot, now), now=now
    )
    params["confirm_code"] = code
    if ok:
        task.status, task.phase, task.progress = "SUCCEEDED", "COMPLETED", 100
        task.completed_at = now
        _event(db, task, {"confirmed": True, "code": code})
    task.parameters_json = params
    return code


def utcnow() -> datetime:
    return datetime.now(UTC)


def process(topic: str, payload: bytes) -> bool:
    """mqtt-ingress entry: returns True when the topic belongs to this namespace."""
    from app.db.session import SessionLocal

    if not topic.startswith(TOPIC_PREFIX):
        return False
    parts = topic.split("/")
    if len(parts) != 4 or parts[3] not in KINDS or not sim_enabled():
        return True
    try:
        body = c.strict_loads(payload)
        with SessionLocal.begin() as db:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == parts[2]).with_for_update())
            if parts[3] == "scene_receipt":
                code = handle_scene_receipt(db, robot, body, utcnow())
            else:
                code = handle_mission_feedback(db, robot, body, utcnow())
        _count(f"{parts[3]}:{code}")
    except SimIngressRefused as exc:
        _count(exc.code)
    except c.ContractError as exc:
        _count(f"CONTRACT:{exc.code}")
    return True
