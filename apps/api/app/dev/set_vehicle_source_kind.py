"""Ops tool: explicit one-time source_kind migration for a vehicle.

Never runs automatically. Usage (inside the api container):

    docker compose -f docker-compose.server.yml exec -T api \
        python -m app.dev.set_vehicle_source_kind firebot-vehicle-01 CANONICAL_MQTT

A source_kind change is a transport-identity fact, never a control-readiness
promotion — every real-control verification flag is always forced false here.
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import select

from app.core.audit import write_audit
from app.core.events import append_event
from app.core.serialization import serialize_model
from app.db.models import Robot, RobotIntegrationProfile
from app.db.session import SessionLocal

ALLOWED = {"CANONICAL_MQTT", "ROS_COMPAT", "MOCK"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m app.dev.set_vehicle_source_kind <vehicle_id> <source_kind>")
        return 2
    vehicle_id, source_kind = args[0], args[1].upper()
    if source_kind not in ALLOWED:
        print(f"ERROR: source_kind must be one of {sorted(ALLOWED)}")
        return 2

    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        if not robot:
            print(f"ERROR: robot not found: {vehicle_id}")
            return 1
        profile = db.get(RobotIntegrationProfile, robot.id)
        if not profile:
            profile = RobotIntegrationProfile(robot_id=robot.id)
            db.add(profile)
        before = profile.source_kind
        profile.source_kind = source_kind
        # source_kind 只是传输身份；绝不据此提升真实控制验证位
        profile.control_contract_verified = False
        profile.ack_contract_verified = False
        profile.map_contract_verified = False
        profile.bidirectional_bridge_verified = False
        profile.command_path_verified = False
        profile.cmd_vel_arbitration_verified = False
        write_audit(
            db,
            action="ROBOT_SOURCE_KIND_MIGRATED",
            resource_type="ROBOT",
            user_id=None,
            robot_id=robot.id,
            resource_id=robot.id,
            before={"source_kind": before},
            after={"source_kind": source_kind},
            actor_type="SYSTEM",
        )
        robot_result = serialize_model(robot)
        profile_result = serialize_model(profile)

    append_event("robot.updated", robot_result)
    print(f"SOURCE_KIND_MIGRATED vehicle={vehicle_id} {before} -> {source_kind}")
    print(json.dumps(profile_result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
