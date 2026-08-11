from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import (
    AppSetting,
    ExtinguishPoint,
    FireEvent,
    InspectionPoint,
    Map,
    MapVersion,
    ParkingSlot,
    Permission,
    Robot,
    RobotCapability,
    Role,
    Site,
    StreamRegistry,
    Task,
    TaskEvent,
    TelemetrySample,
    Trajectory,
    User,
    role_permissions,
    user_roles,
)
from app.db.session import SessionLocal

PERMISSIONS = {
    "robot.read": "查看机器人",
    "robot.control.manual": "手动控制",
    "robot.control.stop": "停止运动",
    "robot.control.estop": "软件急停",
    "robot.control.reset_estop": "复位软件急停",
    "robot.control.force_release": "强制释放租约",
    "robot.control.task": "机器人任务控制",
    "patrol.create": "创建巡检任务",
    "extinguish.create": "创建灭火任务",
    "alarm.read": "查看报警",
    "alarm.ack": "确认报警",
    "alarm.confirm": "确认火情",
    "alarm.dismiss": "排除报警",
    "alarm.resolve": "解决报警",
    "map.read": "查看地图",
    "map.edit": "编辑地图",
    "map.publish": "发布地图",
    "user.manage": "管理用户",
    "role.manage": "管理角色",
    "audit.read": "查看审计",
    "settings.manage": "管理设置",
}

ROLE_GRANTS = {
    "super_admin": list(PERMISSIONS),
    "administrator": list(PERMISSIONS),
    "dispatcher": [
        "robot.read",
        "robot.control.stop",
        "robot.control.estop",
        "robot.control.task",
        "patrol.create",
        "extinguish.create",
        "alarm.read",
        "alarm.ack",
        "alarm.confirm",
        "alarm.dismiss",
        "alarm.resolve",
        "map.read",
        "audit.read",
    ],
    "operator": [
        "robot.read",
        "robot.control.manual",
        "robot.control.stop",
        "robot.control.estop",
        "robot.control.reset_estop",
        "patrol.create",
        "alarm.read",
        "alarm.ack",
        "map.read",
    ],
    "viewer": ["robot.read", "alarm.read", "map.read"],
    "auditor": ["robot.read", "alarm.read", "map.read", "audit.read"],
}


def _get_or_create(db, model, defaults=None, **filters):
    obj = db.scalar(select(model).filter_by(**filters))
    if obj:
        return obj
    obj = model(**filters, **(defaults or {}))
    db.add(obj)
    db.flush()
    return obj


def seed() -> None:
    settings = get_settings()
    password = settings.effective_admin_password
    if not password:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD is required for the first seed")

    with SessionLocal.begin() as db:
        permission_rows = {
            code: _get_or_create(db, Permission, code=code, defaults={"name": name})
            for code, name in PERMISSIONS.items()
        }
        role_rows = {
            code: _get_or_create(db, Role, code=code, defaults={"name": name})
            for code, name in {
                "super_admin": "超级管理员",
                "administrator": "管理员",
                "dispatcher": "调度员",
                "operator": "操作员",
                "viewer": "只读用户",
                "auditor": "审计员",
            }.items()
        }
        for role_code, grants in ROLE_GRANTS.items():
            role = role_rows[role_code]
            for permission_code in grants:
                exists = db.execute(
                    select(role_permissions).where(
                        role_permissions.c.role_id == role.id,
                        role_permissions.c.permission_id == permission_rows[permission_code].id,
                    )
                ).first()
                if not exists:
                    db.execute(
                        insert(role_permissions).values(
                            role_id=role.id, permission_id=permission_rows[permission_code].id
                        )
                    )

        admin = db.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
        if not admin:
            admin = User(
                username=settings.bootstrap_admin_username,
                password_hash=hash_password(password),
                display_name=settings.bootstrap_admin_display_name,
                must_change_password=True,
            )
            db.add(admin)
            db.flush()
        if not db.execute(
            select(user_roles).where(
                user_roles.c.user_id == admin.id,
                user_roles.c.role_id == role_rows["super_admin"].id,
            )
        ).first():
            db.execute(
                insert(user_roles).values(user_id=admin.id, role_id=role_rows["super_admin"].id)
            )

        if not settings.seed_demo:
            print("seed completed: roles and bootstrap admin (demo data disabled)")
            return

        site = _get_or_create(
            db,
            Site,
            code="DEMO_PARKING",
            defaults={"name": "示范停车场", "timezone": "Asia/Shanghai"},
        )
        map_row = _get_or_create(
            db, Map, site_id=site.id, code="parking_v1", defaults={"name": "示范停车场主地图"}
        )
        map_version = _get_or_create(
            db,
            MapVersion,
            map_id=map_row.id,
            version="1",
            defaults={
                "status": "PUBLISHED",
                "checksum": "demo-map-v1",
                "semantic_revision": 1,
                "width_m": 30,
                "height_m": 20,
                "origin_x": 0,
                "origin_y": 0,
                "rotation_rad": 0,
                "resolution_m_per_pixel": 0.05,
                "frame_id": "map",
                "created_by": admin.id,
                "published_at": datetime.now(UTC),
            },
        )
        if map_row.active_version_id != map_version.id:
            map_row.active_version_id = map_version.id

        slots: list[ParkingSlot] = []
        for index in range(12):
            col, row = index % 6, index // 6
            x, y = 3 + col * 4.4, 5 + row * 7.5
            code = f"A-{index + 1:02d}"
            slot = _get_or_create(
                db,
                ParkingSlot,
                map_version_id=map_version.id,
                code=code,
                defaults={
                    "polygon_json": {
                        "points": [
                            {"x": x - 1.5, "y": y - 2.2},
                            {"x": x + 1.5, "y": y - 2.2},
                            {"x": x + 1.5, "y": y + 2.2},
                            {"x": x - 1.5, "y": y + 2.2},
                        ]
                    },
                    "center_pose_json": {"x": x, "y": y, "theta": math.pi / 2},
                    "enabled": True,
                },
            )
            slots.append(slot)
            _get_or_create(
                db,
                InspectionPoint,
                map_version_id=map_version.id,
                parking_slot_id=slot.id,
                defaults={
                    "pose_json": {"x": x - 1.9, "y": y, "theta": 0},
                    "sensor_orientation_json": {"yaw": 0},
                    "priority": 1,
                },
            )
            _get_or_create(
                db,
                ExtinguishPoint,
                map_version_id=map_version.id,
                parking_slot_id=slot.id,
                defaults={
                    "pose_json": {"x": x - 2.3, "y": y, "theta": 0},
                    "approach_json": {"distance_m": 0.5},
                    "nozzle_config_json": {"preset": "PROTOCOL_TODO"},
                },
            )

        path = [{"x": 1 + i * 0.55, "y": 10 + math.sin(i / 5) * 5, "theta": 0} for i in range(50)]
        trajectory = _get_or_create(
            db,
            Trajectory,
            map_version_id=map_version.id,
            code="DEMO_LOOP",
            defaults={"version": "1", "path_json": path, "enabled": True},
        )
        robot = _get_or_create(
            db,
            Robot,
            vehicle_id="R001",
            defaults={
                "site_id": site.id,
                "name": "灭火机器人 R001",
                "model": "FIREBOT-MOCK",
                "current_map_id": map_row.id,
                "current_map_version": "1",
                "battery": 96,
            },
        )
        _get_or_create(
            db,
            RobotCapability,
            robot_id=robot.id,
            defaults={
                "protocol_version": "1.2.0",
                "supported_commands_json": [
                    "manual_control",
                    "stop_motion",
                    "emergency_stop",
                    "reset_estop",
                    "return_dock",
                    "patrol",
                    "extinguish",
                    "cancel_task",
                ],
                "sensors_json": ["smoke", "bottom_ir", "top_ir"],
                "media_json": ["roof_rgb", "roof_thermal", "bottom_ir"],
            },
        )
        for camera_type in ("roof_rgb", "roof_thermal", "bottom_ir"):
            _get_or_create(
                db,
                StreamRegistry,
                stream_id=f"R001-{camera_type}",
                defaults={
                    "robot_id": robot.id,
                    "camera_type": camera_type,
                    "provider": "MEDIAMTX",
                    "playback_url": f"/media/R001-{camera_type}/whep",
                    "codec": "H264",
                    "state": "OFFLINE",
                },
            )

        if not db.scalar(select(FireEvent).where(FireEvent.event_code == "FE-DEMO-RESOLVED")):
            now = datetime.now(UTC)
            db.add(
                FireEvent(
                    event_code="FE-DEMO-RESOLVED",
                    robot_id=robot.id,
                    parking_slot_id=slots[2].id,
                    detection_method="AUTO",
                    fire_type="smoke",
                    confidence=0.88,
                    severity="MEDIUM",
                    fingerprint="demo-resolved",
                    state="RESOLVED",
                    first_seen_at=now - timedelta(hours=2),
                    last_seen_at=now - timedelta(hours=2),
                    occurrence_count=3,
                    resolved_at=now - timedelta(hours=1, minutes=40),
                    source_position_json=slots[2].center_pose_json,
                )
            )
        sample_task = db.scalar(select(Task).where(Task.task_code == "T-DEMO-COMPLETED"))
        if not sample_task:
            sample_task = Task(
                task_code="T-DEMO-COMPLETED",
                robot_id=robot.id,
                type="PATROL",
                status="SUCCEEDED",
                phase="COMPLETED",
                progress=100,
                target_parking_slot_id=slots[0].id,
                target_pose_snapshot_json=slots[0].center_pose_json,
                map_id_snapshot=map_row.id,
                map_version_snapshot="1",
                semantic_revision_snapshot=1,
                trajectory_snapshot_json=trajectory.path_json,
                parameters_json={},
                created_by=admin.id,
                accepted_at=datetime.now(UTC) - timedelta(hours=1),
                started_at=datetime.now(UTC) - timedelta(minutes=55),
                completed_at=datetime.now(UTC) - timedelta(minutes=45),
            )
            db.add(sample_task)
            db.flush()
            db.add(
                TaskEvent(
                    task_id=sample_task.id, status="SUCCEEDED", phase="COMPLETED", progress=100
                )
            )
        if not db.scalar(select(TelemetrySample).where(TelemetrySample.robot_id == robot.id)):
            now = datetime.now(UTC)
            for i in range(20):
                ts = now - timedelta(seconds=20 - i)
                db.add(
                    TelemetrySample(
                        robot_id=robot.id,
                        source_timestamp=ts,
                        server_received_at=ts,
                        x=1 + i * 0.3,
                        y=10 + math.sin(i / 4),
                        theta=0.2,
                        linear_speed=0.1,
                        angular_speed=0,
                        battery=96 - i * 0.02,
                        localization_status="OK",
                        map_version="1",
                        boot_id="seed-history",
                        seq=i,
                    )
                )
        for key, value in {
            "retention": {"telemetry_days": 30, "sensor_days": 90, "audit_days": 365},
            "system": {"site": "DEMO_PARKING", "server_deployed": False},
        }.items():
            _get_or_create(
                db, AppSetting, key=key, defaults={"value_json": value, "updated_by": admin.id}
            )

    print("seed completed: roles, bootstrap admin, DEMO_PARKING, parking_v1, R001")


if __name__ == "__main__":
    seed()
