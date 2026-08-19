#!/usr/bin/env python3
"""Idempotent DEMO_PARKING navigation sync (right-side S-cruise).

Backfills navigation presets for existing dev databases that predate the
right-side S-cruise geometry. It uses the exact same route builder as
`app.db.seed`, so fresh-seed and sync can never drift.

Usage (from repo root, inside the api venv):
    python scripts/sync_demo_navigation.py             # dry-run
    python scripts/sync_demo_navigation.py --apply     # write changes (dev only)

Safety:
- refuses to write when app_env != "dev" unless --force-server
- only touches the DEMO_PARKING demo map, never a production map
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "api"))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Map,
    MapVersion,
    NavigationPreset,
    ParkingSlot,
    PatrolPlan,
    PatrolPlanPoint,
    Robot,
    Trajectory,
)
from app.db.session import SessionLocal  # noqa: E402
from app.modules.navigation.route_builder import (  # noqa: E402
    REMOTE_WAITING,
    SlotRef,
    build_cruise_trajectory,
    inspection_pose,
    ordered_codes,
)


def sync(db, apply: bool) -> dict:
    report: dict = {"presets_created": 0, "presets_bound": 0, "trajectory": "", "plan_points": 0}

    map_version = db.scalar(
        select(MapVersion)
        .join(Map, Map.id == MapVersion.map_id)
        .where(Map.code == "parking_v1", MapVersion.status == "PUBLISHED")
        .order_by(MapVersion.created_at.desc())
    )
    robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
    if not map_version or not robot:
        report["error"] = "demo map version or R001 robot not found"
        return report

    slots = list(
        db.scalars(select(ParkingSlot).where(ParkingSlot.map_version_id == map_version.id)).all()
    )
    if len(slots) != 54:
        report["error"] = f"expected 54 slots, found {len(slots)}"
        return report

    slot_refs = [
        SlotRef(code=s.code, x=s.center_pose_json["x"], y=s.center_pose_json["y"]) for s in slots
    ]
    pose_by_code = {s.code: inspection_pose(s) for s in slot_refs}

    for slot in slots:
        preset = db.scalar(
            select(NavigationPreset).where(
                NavigationPreset.map_version_id == map_version.id,
                NavigationPreset.code == f"INSPECT_{slot.code.replace('-', '_')}",
            )
        )
        if not preset:
            if apply:
                preset = NavigationPreset(
                    map_version_id=map_version.id,
                    parking_slot_id=slot.id,
                    code=f"INSPECT_{slot.code.replace('-', '_')}",
                    name=f"{slot.code} 巡检位",
                    category="INSPECTION",
                    pose_json=pose_by_code[slot.code],
                    position_tolerance_m=0.2,
                    yaw_tolerance_rad=0.15,
                    allowed_approach_json={"direction": "FORWARD_ONLY"},
                    requires_reverse=False,
                    is_default=False,
                    enabled=True,
                    semantic_revision=map_version.semantic_revision,
                )
                db.add(preset)
            report["presets_created"] += 1
        else:
            if preset.parking_slot_id != slot.id:
                report["presets_bound"] += 1
                if apply:
                    preset.parking_slot_id = slot.id
            if apply:
                preset.pose_json = pose_by_code[slot.code]
                preset.semantic_revision = map_version.semantic_revision

    waiting = db.scalar(
        select(NavigationPreset).where(
            NavigationPreset.map_version_id == map_version.id,
            NavigationPreset.code == "REMOTE_WAITING_AREA",
        )
    )
    if not waiting and apply:
        db.add(
            NavigationPreset(
                map_version_id=map_version.id,
                code="REMOTE_WAITING_AREA",
                name="远端待命区",
                category="WAITING_AREA",
                pose_json=dict(REMOTE_WAITING),
                position_tolerance_m=0.25,
                yaw_tolerance_rad=0.18,
                allowed_approach_json={"direction": "FORWARD_ONLY"},
                is_default=True,
                enabled=True,
                semantic_revision=map_version.semantic_revision,
            )
        )
    elif waiting and apply:
        waiting.pose_json = dict(REMOTE_WAITING)

    path = build_cruise_trajectory(slot_refs)
    trajectory = db.scalar(
        select(Trajectory).where(
            Trajectory.map_version_id == map_version.id,
            Trajectory.code == "RIGHT_SIDE_S_CRUISE",
        )
    )
    if not trajectory:
        if apply:
            trajectory = Trajectory(
                map_version_id=map_version.id,
                code="RIGHT_SIDE_S_CRUISE",
                version="2",
                path_json=path,
                enabled=True,
            )
            db.add(trajectory)
            db.flush()
        report["trajectory"] = "create"
    else:
        report["trajectory"] = "update"
        if apply:
            trajectory.path_json = path
            trajectory.enabled = True

    plan = db.scalar(select(PatrolPlan).where(PatrolPlan.code == "RIGHT_SIDE_S_CRUISE_PLAN"))
    ordered = ordered_codes(slot_refs)
    if plan:
        report["plan_points"] = len(ordered)
        if apply and trajectory:
            plan.trajectory_id = trajectory.id
            plan.enabled = True

    if apply:
        db.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="sync demo navigation")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force-server", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    print(f"app_env = {settings.app_env}")
    if args.apply and settings.app_env != "dev" and not args.force_server:
        print("REFUSED: --apply requires app_env == 'dev' (or --force-server).")
        return 2

    with SessionLocal.begin() as db:
        report = sync(db, apply=args.apply)
    print("sync report:", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
