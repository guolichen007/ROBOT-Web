"""WEB-R1-MIN 单组织单站点访问范围（不是通用多租户框架）。

规则：
- 持有 ``settings.manage`` 的管理员不受站点限制（运维/跨站点排障）。
- 其他账号只能访问 *运营站点* 的车辆，以及与这些车辆 / 该站点地图关联的告警、任务、
  命令、媒体、历史与导出。
- 运营站点 = ``OPERATING_SITE_CODE``；未配置时取 code 排序第一个 active Site
  （与 /monitor/snapshot 的地图选择一致）。找不到时普通账号范围为空（fail-closed）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import PlatformError
from app.db.models import FireEvent, Map, MapVersion, ParkingSlot, Robot, Site

if TYPE_CHECKING:
    from app.core.dependencies import AuthContext

UNRESTRICTED_PERMISSION = "settings.manage"


def operating_site(db) -> Site | None:
    code = get_settings().operating_site_code.strip()
    if code:
        return db.scalar(select(Site).where(Site.code == code))
    return db.scalar(select(Site).where(Site.active.is_(True)).order_by(Site.code))


def unrestricted(permissions: set[str] | list[str]) -> bool:
    return UNRESTRICTED_PERMISSION in set(permissions)


def allowed_site_ids(db, permissions: set[str] | list[str]) -> set[str] | None:
    """None = 不限站点；空集合 = 无可访问站点。"""
    if unrestricted(permissions):
        return None
    site = operating_site(db)
    return {site.id} if site else set()


def scoped_robot_ids(db, permissions: set[str] | list[str]) -> set[str] | None:
    sites = allowed_site_ids(db, permissions)
    if sites is None:
        return None
    if not sites:
        return set()
    return set(db.scalars(select(Robot.id).where(Robot.site_id.in_(sites))).all())


def robot_visible(db, permissions: set[str] | list[str], robot: Robot) -> bool:
    sites = allowed_site_ids(db, permissions)
    return sites is None or robot.site_id in sites


def assert_robot_visible(db, auth: AuthContext, robot: Robot) -> None:
    # 越权访问返回 404（与不存在一致），避免泄露其他站点车辆是否存在。
    if not robot_visible(db, auth.permissions, robot):
        raise PlatformError("RESOURCE_NOT_FOUND", "机器人不存在", status_code=404)


def slot_site_id(db, slot_id: str | None) -> str | None:
    if not slot_id:
        return None
    return db.scalar(
        select(Map.site_id)
        .join(MapVersion, MapVersion.map_id == Map.id)
        .join(ParkingSlot, ParkingSlot.map_version_id == MapVersion.id)
        .where(ParkingSlot.id == slot_id)
    )


def alarm_visible(db, permissions: set[str] | list[str], alarm: FireEvent) -> bool:
    sites = allowed_site_ids(db, permissions)
    if sites is None:
        return True
    if alarm.robot_id:
        robot = db.get(Robot, alarm.robot_id)
        return bool(robot and robot.site_id in sites)
    return slot_site_id(db, alarm.parking_slot_id) in sites


def alarm_scope_clause(db, permissions: set[str] | list[str]):
    """SQL 条件：普通账号只看本站点车辆或本站点车位上的火警。None = 不限。"""
    sites = allowed_site_ids(db, permissions)
    if sites is None:
        return None
    robot_ids = select(Robot.id).where(Robot.site_id.in_(sites))
    slot_ids = (
        select(ParkingSlot.id)
        .join(MapVersion, MapVersion.id == ParkingSlot.map_version_id)
        .join(Map, Map.id == MapVersion.map_id)
        .where(Map.site_id.in_(sites))
    )
    return (FireEvent.robot_id.in_(robot_ids)) | (
        FireEvent.robot_id.is_(None) & FireEvent.parking_slot_id.in_(slot_ids)
    )
