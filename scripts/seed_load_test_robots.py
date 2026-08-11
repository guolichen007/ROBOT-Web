from __future__ import annotations

from app.core.config import get_settings
from app.db.models import Robot, Site
from app.db.session import SessionLocal
from sqlalchemy import select


def main() -> None:
    if get_settings().app_env != "test":
        raise SystemExit("load-test robot provisioning is allowed only in APP_ENV=test")
    created = 0
    with SessionLocal.begin() as db:
        site = db.scalar(select(Site).where(Site.code == "DEMO_PARKING"))
        if not site:
            raise SystemExit("DEMO_PARKING seed is required")
        for index in range(1, 11):
            vehicle_id = f"LOAD{index:03d}"
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
            if robot:
                robot.enabled = True
                continue
            db.add(
                Robot(
                    vehicle_id=vehicle_id,
                    site_id=site.id,
                    name=f"负载测试机器人 {index:02d}",
                    model="PROTOCOL_LOAD_TEST_ONLY",
                    enabled=True,
                )
            )
            created += 1
    print(f"LOAD_TEST_ROBOTS_READY=10 CREATED={created}")


if __name__ == "__main__":
    main()
