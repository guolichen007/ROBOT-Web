"""Register the WEB-R1B SIM acceptance vehicle `firebot-sim-r002-01` (isolated test stack ONLY).

Clones the seeded R002 test configuration (capability / integration / motion profile / data
channels) under the SIM namespace. Refuses to run outside APP_ENV=test with
FIREBOT_DEPLOYMENT_ENV=SIM, so it can never create a SIM identity in a REAL deployment.
usage: python -m app.dev.setup_sim_r002
"""

from __future__ import annotations

import os

from sqlalchemy import select

from app.core.config import get_settings, is_synthetic_namespace
from app.db.models import (
    Robot,
    RobotCapability,
    RobotDataChannel,
    RobotIntegrationProfile,
    RobotMotionProfile,
)
from app.db.session import SessionLocal

SIM_ID = "firebot-sim-r002-01"


def main() -> None:
    settings = get_settings()
    if os.environ.get("APP_ENV") != "test" or settings.firebot_deployment_env != "SIM":
        raise SystemExit("REFUSED: requires APP_ENV=test and FIREBOT_DEPLOYMENT_ENV=SIM")
    assert is_synthetic_namespace(SIM_ID)
    with SessionLocal.begin() as db:
        src = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        if src is None:
            raise SystemExit("seeded R002 template missing")
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM_ID))
        if robot is None:
            robot = Robot(
                vehicle_id=SIM_ID,
                site_id=src.site_id,
                name="SIM 灭火机器人 R002（Gazebo/模拟）",
                model="FIREBOT-SIM",
                current_map_id=src.current_map_id,
                current_map_version=src.current_map_version,
                agent_protocol="CANONICAL_MQTT",
            )
            db.add(robot)
            db.flush()
        for model in (RobotCapability, RobotIntegrationProfile, RobotMotionProfile):
            template = db.get(model, src.id)
            if template is None or db.get(model, robot.id) is not None:
                continue
            values = {
                c.name: getattr(template, c.name)
                for c in model.__table__.columns
                if c.name != "robot_id"
            }
            db.add(model(robot_id=robot.id, **values))
        existing = {
            ch.channel
            for ch in db.scalars(
                select(RobotDataChannel).where(RobotDataChannel.robot_id == robot.id)
            )
        }
        for ch in db.scalars(select(RobotDataChannel).where(RobotDataChannel.robot_id == src.id)):
            if ch.channel in existing:
                continue
            values = {
                c.name: getattr(ch, c.name)
                for c in RobotDataChannel.__table__.columns
                if c.name not in {"id", "robot_id"}
            }
            db.add(RobotDataChannel(robot_id=robot.id, **values))
    print(f"SIM_VEHICLE_READY {SIM_ID}")


if __name__ == "__main__":
    main()
