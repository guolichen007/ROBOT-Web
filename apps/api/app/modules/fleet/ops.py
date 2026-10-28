"""Fleet 受控操作服务层（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE）。

服务器侧 `firebotctl fleet sites` / `firebotctl fleet preregister` 的唯一受控实现：
全部经 SQLAlchemy model 操作，禁止调用方现场手写 SQL / 内联逻辑。

preregister 边界（fail-closed）：
  - 只建立 Fleet stable identity（Robot row，enabled=false）+ 最小只读 assignment 基线。
  - 绝不：生成 MQTT password / 修改 Mosquitto / 签发 enrollment 或 device token /
    生成 SRT credential / 打开 downlink / 修改 allowlist / 启动车辆 / 创建任务 /
    修改任何已存在 Robot（幂等/冲突一律不自动更新）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Robot, RobotFleetAssignment, Site
from app.db.session import SessionLocal

DEFAULT_PROFILE_ID = "firebot_ros1_standard_v1"
CANONICAL_PROTOCOL = "CANONICAL_MQTT"


@dataclass
class PreregisterResult:
    """preregister 结果：CREATED / EXISTS / CONFLICT / REJECTED。"""

    status: str
    detail: str | None = None
    robot: Robot | None = None


def list_sites(db: Session) -> list[dict[str, object]]:
    """只读列出全部 Site（不含任何敏感字段）。"""
    rows: list[dict[str, object]] = []
    for site in db.scalars(select(Site).order_by(Site.code)).all():
        robot_count = db.scalar(select(func.count(Robot.id)).where(Robot.site_id == site.id)) or 0
        rows.append(
            {
                "site_code": site.code,
                "site_name": site.name,
                "active": site.active,
                "robot_count": robot_count,
            }
        )
    return rows


def preregister(db: Session, device_id: str, name: str, site_code: str) -> PreregisterResult:
    """建立 Fleet stable identity（幂等；冲突一律拒绝，不自动更新）。

    新 Robot 固定基线：enabled=false（允许 MQTT 上行/遥测，控制默认关闭）、
    online_state=OFFLINE、current_mode=IDLE、agent_protocol=CANONICAL_MQTT。
    最小 assignment：supported_commands=[]、location_enabled=false、无 device_token_hash。
    """
    site = db.scalar(select(Site).where(Site.code == site_code))
    if site is None:
        return PreregisterResult("REJECTED", "SITE_NOT_FOUND")
    if not site.active:
        return PreregisterResult("REJECTED", "SITE_INACTIVE")

    existing = db.scalar(select(Robot).where(Robot.vehicle_id == device_id))
    if existing is not None:
        # 幂等：vehicle_id/site/name 完全一致才 EXISTS；任何不一致 → CONFLICT，不自动更新。
        if existing.site_id != site.id:
            return PreregisterResult("CONFLICT", "SITE_MISMATCH", existing)
        if existing.name != name:
            return PreregisterResult("CONFLICT", "NAME_MISMATCH", existing)
        return PreregisterResult("EXISTS", "IDENTICAL", existing)

    robot = Robot(
        vehicle_id=device_id,
        name=name,
        site_id=site.id,
        agent_protocol=CANONICAL_PROTOCOL,
        online_state="OFFLINE",
        current_mode="IDLE",
        enabled=False,
    )
    db.add(robot)
    db.flush()  # 生成 robot.id 供 assignment 外键使用
    db.add(
        RobotFleetAssignment(
            robot_id=robot.id,
            profile_id=DEFAULT_PROFILE_ID,
            location_enabled=False,
            supported_commands_json=[],
        )
    )
    db.commit()
    db.refresh(robot)
    return PreregisterResult("CREATED", None, robot)


def _print_identity(res: PreregisterResult, device_id: str, name: str, site_code: str) -> None:
    """输出身份与只读基线（仅非敏感字段）。"""
    print(f"vehicle_id={device_id}")
    print(f"name={name}")
    print(f"site={site_code}")
    print("PLATFORM_ENABLED=false")
    print("SUPPORTED_COMMANDS=[]")
    print("LOCATION_ENABLED=false")


def run_cli(argv: list[str]) -> int:
    """受控 CLI（在 api 容器内执行）：sites | preregister <id> --name N --site-code S。"""
    if not argv:
        print("用法: fleet ops <sites|preregister ...>", file=sys.stderr)
        return 2
    sub = argv[0]
    if sub == "sites":
        db = SessionLocal()
        try:
            rows = list_sites(db)
        finally:
            db.close()
        for r in rows:
            active = "true" if r["active"] else "false"
            print(
                f"SITE_CODE={r['site_code']} SITE_NAME={r['site_name']} "
                f"ACTIVE={active} ROBOT_COUNT={r['robot_count']}"
            )
        return 0

    if sub == "preregister":
        device_id = ""
        name = ""
        site_code = ""
        if len(argv) < 2 or not argv[1]:
            print(
                "用法: fleet ops preregister <DEVICE_ID> --name <N> --site-code <S>",
                file=sys.stderr,
            )
            return 2
        device_id = argv[1]
        rest = argv[2:]
        i = 0
        while i < len(rest):
            if rest[i] == "--name" and i + 1 < len(rest):
                name = rest[i + 1]
                i += 2
            elif rest[i] == "--site-code" and i + 1 < len(rest):
                site_code = rest[i + 1]
                i += 2
            else:
                i += 1
        if not device_id or not name or not site_code:
            print(
                "ERROR: 必须显式提供 --name 与 --site-code（禁止猜测/默认 Site）",
                file=sys.stderr,
            )
            return 2

        db = SessionLocal()
        try:
            res = preregister(db, device_id, name, site_code)
        finally:
            db.close()

        if res.status == "REJECTED":
            print("PREREGISTER=REJECTED")
            print(res.detail)
            return 1
        if res.status == "CONFLICT":
            print("PREREGISTER=CONFLICT")
            print(res.detail)
            return 1
        print(f"PREREGISTER={res.status}")
        _print_identity(res, device_id, name, site_code)
        return 0

    print(f"未知子命令: {sub}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
