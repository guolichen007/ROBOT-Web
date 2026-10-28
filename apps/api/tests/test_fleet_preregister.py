"""fleet preregister 服务层合同测试（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE / P0-B）。

覆盖：
  UNKNOWN_SITE=REJECT / INACTIVE_SITE=REJECT
  FIRST_CREATE=PASS / SECOND_IDENTICAL=IDEMPOTENT
  SITE_CONFLICT=REJECT / IDENTITY_CONFLICT=REJECT
  PLATFORM_ENABLED=false / SUPPORTED_COMMANDS=[] / LOCATION_ENABLED=false
  NO_CREDENTIAL_CREATED=PASS / NO_ENROLL_TOKEN_CREATED=PASS
  其他 Robot（含测试账号）不被触碰（R002_TEST_ACCOUNT_UNCHANGED 语义）

使用独立 fixture 命名空间 + 测试前后清理；不依赖/不污染生产数据。
"""

from __future__ import annotations

import uuid

from app.db.models import (
    EnrollmentToken,
    Robot,
    RobotCredential,
    RobotFleetAssignment,
    Site,
)
from app.db.session import SessionLocal
from app.modules.fleet.ops import list_sites, preregister
from sqlalchemy import delete, select

NS = "preregtest"
DEVICE_ID = f"{NS}-vehicle-{uuid.uuid4().hex[:8]}"
DISPLAY_NAME = "现场测试车"
ACTIVE_SITE_CODE = f"{NS}-site-{uuid.uuid4().hex[:8]}"
INACTIVE_SITE_CODE = f"{NS}-site-inactive-{uuid.uuid4().hex[:8]}"
OTHER_SITE_CODE = f"{NS}-site-other-{uuid.uuid4().hex[:8]}"
OTHER_DEVICE_ID = f"{NS}-other-{uuid.uuid4().hex[:8]}"


def _cleanup() -> None:
    with SessionLocal.begin() as db:
        for vehicle_id in (DEVICE_ID, OTHER_DEVICE_ID):
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
            if robot is not None:
                db.execute(
                    delete(RobotFleetAssignment).where(RobotFleetAssignment.robot_id == robot.id)
                )
                db.execute(delete(RobotCredential).where(RobotCredential.robot_id == robot.id))
                db.execute(delete(Robot).where(Robot.id == robot.id))
        for code in (ACTIVE_SITE_CODE, INACTIVE_SITE_CODE, OTHER_SITE_CODE):
            site = db.scalar(select(Site).where(Site.code == code))
            if site is not None:
                db.execute(delete(Site).where(Site.id == site.id))
        db.execute(delete(EnrollmentToken).where(EnrollmentToken.device_id == DEVICE_ID))


def _setup() -> None:
    _cleanup()
    with SessionLocal.begin() as db:
        db.add(Site(code=ACTIVE_SITE_CODE, name="活动现场", active=True))
        db.add(Site(code=INACTIVE_SITE_CODE, name="停用现场", active=False))
        db.add(Site(code=OTHER_SITE_CODE, name="另一现场", active=True))
        db.flush()  # SessionLocal 为 autoflush=False，add 后必须先 flush 才能查询
        other_site = db.scalar(select(Site).where(Site.code == OTHER_SITE_CODE))
        assert other_site is not None
        other_site_id = other_site.id
        # 旁置“其他车辆”模拟已存在测试账号，用于验证 preregister 不触碰任何他人
        db.add(
            Robot(
                vehicle_id=OTHER_DEVICE_ID,
                name="既有测试账号",
                site_id=other_site_id,
            )
        )


def _session():
    """普通 Session（服务层 preregister 自包含 commit，不用 begin() 上下文）。"""
    return SessionLocal()


def _robot(vehicle_id: str = DEVICE_ID) -> Robot | None:
    with SessionLocal.begin() as db:
        return db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))


def test_unknown_site_rejected() -> None:
    _setup()
    db = _session()
    try:
        res = preregister(db, DEVICE_ID, DISPLAY_NAME, f"{NS}-missing-site")
    finally:
        db.close()
    assert res.status == "REJECTED"
    assert res.detail == "SITE_NOT_FOUND"
    assert _robot() is None
    _cleanup()


def test_inactive_site_rejected() -> None:
    _setup()
    db = _session()
    try:
        res = preregister(db, DEVICE_ID, DISPLAY_NAME, INACTIVE_SITE_CODE)
    finally:
        db.close()
    assert res.status == "REJECTED"
    assert res.detail == "SITE_INACTIVE"
    assert _robot() is None
    _cleanup()


def test_first_create_baseline() -> None:
    _setup()
    db = _session()
    try:
        res = preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE)
    finally:
        db.close()
    assert res.status == "CREATED"
    robot = _robot()
    assert robot is not None
    assert robot.vehicle_id == DEVICE_ID
    assert robot.name == DISPLAY_NAME
    assert robot.enabled is False
    assert robot.online_state == "OFFLINE"
    assert robot.current_mode == "IDLE"
    assert robot.agent_protocol == "CANONICAL_MQTT"
    with SessionLocal.begin() as db:
        site = db.scalar(select(Site).where(Site.code == ACTIVE_SITE_CODE))
        assert site is not None
        assert robot.site_id == site.id
        assignment = db.get(RobotFleetAssignment, robot.id)
        assert assignment is not None
        assert assignment is not None
        assert assignment.supported_commands_json == []
        assert assignment.location_enabled is False
        assert assignment.device_token_hash is None
    _cleanup()


def test_second_identical_idempotent() -> None:
    _setup()
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
    finally:
        db.close()
    db = _session()
    try:
        res = preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE)
    finally:
        db.close()
    assert res.status == "EXISTS"
    assert res.detail == "IDENTICAL"
    robot = _robot()
    assert robot is not None and robot.vehicle_id == DEVICE_ID
    _cleanup()


def test_site_conflict_rejected() -> None:
    _setup()
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
        res = preregister(db, DEVICE_ID, DISPLAY_NAME, OTHER_SITE_CODE)
    finally:
        db.close()
    assert res.status == "CONFLICT"
    assert res.detail == "SITE_MISMATCH"
    robot = _robot()
    assert robot is not None
    with SessionLocal.begin() as db:
        site = db.scalar(select(Site).where(Site.code == ACTIVE_SITE_CODE))
        assert site is not None
        assert robot.site_id == site.id  # 未被自动更新
    _cleanup()


def test_identity_conflict_rejected() -> None:
    _setup()
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
        res = preregister(db, DEVICE_ID, "另一个名字", ACTIVE_SITE_CODE)
    finally:
        db.close()
    assert res.status == "CONFLICT"
    assert res.detail == "NAME_MISMATCH"
    robot = _robot()
    assert robot is not None and robot.name == DISPLAY_NAME  # 未被自动更新
    _cleanup()


def test_no_credential_and_no_token_created() -> None:
    _setup()
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
    finally:
        db.close()
    robot = _robot()
    assert robot is not None
    with SessionLocal.begin() as db:
        creds = db.scalars(
            select(RobotCredential).where(RobotCredential.robot_id == robot.id)
        ).all()
        tokens = db.scalars(
            select(EnrollmentToken).where(EnrollmentToken.device_id == DEVICE_ID)
        ).all()
        assignment = db.get(RobotFleetAssignment, robot.id)
        assert assignment is not None
    assert creds == []
    assert tokens == []
    assert assignment.device_token_hash is None  # 未签发 device token
    _cleanup()


def test_other_robot_untouched() -> None:
    """preregister 不触碰任何已存在 Robot（含测试账号语义）。"""
    _setup()
    before = _robot(OTHER_DEVICE_ID)
    assert before is not None
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
    finally:
        db.close()
    after = _robot(OTHER_DEVICE_ID)
    assert after is not None
    assert before.vehicle_id == after.vehicle_id
    assert before.name == after.name
    assert before.enabled == after.enabled
    assert before.online_state == after.online_state
    assert before.updated_at == after.updated_at  # 未触发 onupdate → 未被修改
    _cleanup()


def test_list_sites_readonly_shape() -> None:
    _setup()
    db = _session()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, ACTIVE_SITE_CODE).status == "CREATED"
        rows = list_sites(db)
    finally:
        db.close()
    ours = [r for r in rows if r["site_code"] == ACTIVE_SITE_CODE]
    assert len(ours) == 1
    assert set(ours[0]) == {"site_code", "site_name", "active", "robot_count"}
    assert ours[0]["active"] is True
    assert ours[0]["robot_count"] == 1
    _cleanup()
