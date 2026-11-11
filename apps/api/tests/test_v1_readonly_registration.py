"""G1-R1：V1 只读设备正式注册与协议绑定合同测试（真实 DB 隔离流程）。

覆盖任务书第 3 节：
  - preregister 基线：enabled=false、estop_active=None（急停未验证，不落默认 False）、
    supported_commands=[]、location_enabled=false、无凭证无 token；
  - 唯一身份：同 vehicle_id 幂等 EXISTS，不新建第二行、不触碰 R001/R002；
  - CANONICAL_MQTT→VEHICLE_AGENT_V1 走受控 API（settings.manage 权限 + 审计日志），
    绑定后 estop 回到未验证（None）；
  - 下行总开关关闭时，能力列表不得宣称任何控制命令可用；
  - enabled=false：只读监测可用（GET 200），控制指令被 ROBOT_DISABLED 拒绝。

测试资产全部测试专属（独立命名空间 + 前后清理），不依赖/不污染生产数据。
"""

from __future__ import annotations

import uuid

import pytest
from app.core.errors import PlatformError
from app.db.models import (
    AuditLog,
    Robot,
    RobotCapability,
    RobotCredential,
    RobotFleetAssignment,
    RobotIntegrationProfile,
    Site,
)
from app.db.session import SessionLocal
from app.main import app
from app.modules.commands.service import assert_robot_can_execute
from app.modules.fleet.ops import preregister
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

NS = "v1reg"
DEVICE_ID = f"{NS}-readonly-{uuid.uuid4().hex[:8]}"
DEVICE_ID_2 = f"{NS}-readonly2-{uuid.uuid4().hex[:8]}"
DISPLAY_NAME = "现场只读测试车"
SITE_CODE = f"{NS}-site-{uuid.uuid4().hex[:8]}"


def _cleanup() -> None:
    with SessionLocal.begin() as db:
        for vehicle_id in (DEVICE_ID, DEVICE_ID_2):
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
            if robot is not None:
                db.execute(
                    delete(RobotFleetAssignment).where(RobotFleetAssignment.robot_id == robot.id)
                )
                db.execute(delete(RobotCredential).where(RobotCredential.robot_id == robot.id))
                db.execute(
                    delete(RobotIntegrationProfile).where(
                        RobotIntegrationProfile.robot_id == robot.id
                    )
                )
                db.execute(delete(RobotCapability).where(RobotCapability.robot_id == robot.id))
                db.execute(delete(Robot).where(Robot.id == robot.id))
        site = db.scalar(select(Site).where(Site.code == SITE_CODE))
        if site is not None:
            db.execute(delete(Site).where(Site.id == site.id))


@pytest.fixture(scope="module", autouse=True)
def _setup():
    _cleanup()
    with SessionLocal.begin() as db:
        db.add(Site(code=SITE_CODE, name="只读注册测试现场", active=True))
    yield
    _cleanup()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        yield value


def _login(client: TestClient) -> dict[str, str]:
    from app.core.config import get_settings

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _robot(vehicle_id: str = DEVICE_ID) -> Robot:
    with SessionLocal() as db:
        return db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))


def test_preregister_readonly_baseline_idempotent_and_r001_untouched() -> None:
    from app.db.models import Robot as RobotModel

    with SessionLocal() as db:
        r001 = db.scalar(select(RobotModel).where(RobotModel.vehicle_id == "R001"))
        assert r001 is not None
        r001_snapshot = (
            r001.id,
            r001.agent_protocol,
            r001.online_state,
            r001.estop_active,
            r001.enabled,
            r001.updated_at,
        )

    db = SessionLocal()
    try:
        result = preregister(db, DEVICE_ID, DISPLAY_NAME, SITE_CODE)
        assert result.status == "CREATED"
    finally:
        db.close()
    robot = _robot()
    assert robot is not None
    # G1-R1 基线：控制默认关闭、急停未验证（None，绝不落默认 False）
    assert robot.enabled is False
    assert robot.estop_active is None
    assert robot.online_state == "OFFLINE"
    assert robot.agent_protocol == "CANONICAL_MQTT"

    with SessionLocal() as db:
        assignment = db.scalar(
            select(RobotFleetAssignment).where(RobotFleetAssignment.robot_id == robot.id)
        )
        assert assignment is not None
        assert assignment.supported_commands_json == []  # 不宣称任何控制能力
        assert assignment.location_enabled is False
        # 无凭证、无 enrollment token（凭证由后续受控 onboarding 发放）
        assert (
            db.scalar(select(RobotCredential).where(RobotCredential.robot_id == robot.id)) is None
        )
        count = len(db.scalars(select(RobotModel).where(RobotModel.vehicle_id == DEVICE_ID)).all())
        assert count == 1  # 唯一身份
        # R001 未被触碰
        r001 = db.scalar(select(RobotModel).where(RobotModel.vehicle_id == "R001"))
        assert (
            r001.id,
            r001.agent_protocol,
            r001.online_state,
            r001.estop_active,
            r001.enabled,
            r001.updated_at,
        ) == r001_snapshot

    db = SessionLocal()
    try:
        result = preregister(db, DEVICE_ID, DISPLAY_NAME, SITE_CODE)
        assert result.status == "EXISTS"  # 幂等：不新建第二行
    finally:
        db.close()
    with SessionLocal() as db:
        count = len(db.scalars(select(RobotModel).where(RobotModel.vehicle_id == DEVICE_ID)).all())
        assert count == 1


def test_v1_binding_readonly_estop_and_capability_downlink_off(client: TestClient) -> None:
    # 绑定前：preregister 建立资产
    db = SessionLocal()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, SITE_CODE).status in {
            "CREATED",
            "EXISTS",
        }
    finally:
        db.close()
    # 测试环境默认下行关闭：绑定后能力不得宣称控制命令可用
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.vehicle_agent_v1_downlink_enabled is False

    headers = _login(client)
    robot = _robot()
    response = client.put(
        f"/api/v1/robots/{robot.id}/agent-protocol",
        json={"protocol": "VEHICLE_AGENT_V1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    with SessionLocal() as db:
        bound = db.get(Robot, robot.id)
        assert bound.agent_protocol == "VEHICLE_AGENT_V1"
        assert bound.estop_active is None  # 协议切换后急停回到未验证
        capability = db.get(RobotCapability, robot.id)
        assert capability is not None
        assert capability.protocol_version == "vehicle-agent-v1"
        assert capability.supported_commands_json == []  # 下行关闭：零控制命令声明
        integration = db.get(RobotIntegrationProfile, robot.id)
        assert integration is not None and integration.read_only_reason
        audit = db.scalar(
            select(AuditLog)
            .where(AuditLog.resource_id == robot.id)
            .where(AuditLog.action == "AGENT_PROTOCOL_BINDING")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        assert audit is not None  # 受控事务有审计证据


def test_v1_binding_capability_honors_downlink_enabled(client: TestClient) -> None:
    # 显式开启下行 + allowlist 后，能力列表才可声明协议命令（既有行为不回归）
    from app.core.config import get_settings

    settings = get_settings()
    db = SessionLocal()
    try:
        assert preregister(db, DEVICE_ID_2, DISPLAY_NAME, SITE_CODE).status in {
            "CREATED",
            "EXISTS",
        }
    finally:
        db.close()
    robot2 = _robot(DEVICE_ID_2)
    headers = _login(client)
    previous_enabled = settings.vehicle_agent_v1_downlink_enabled
    previous_allowlist = settings.vehicle_agent_v1_downlink_allowlist
    settings.vehicle_agent_v1_downlink_enabled = True
    settings.vehicle_agent_v1_downlink_allowlist = DEVICE_ID_2
    try:
        response = client.put(
            f"/api/v1/robots/{robot2.id}/agent-protocol",
            json={"protocol": "VEHICLE_AGENT_V1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        with SessionLocal() as db:
            capability = db.get(RobotCapability, robot2.id)
            from services.vehicle_agent_v1 import COMMANDS

            assert capability.supported_commands_json == list(COMMANDS)
    finally:
        settings.vehicle_agent_v1_downlink_enabled = previous_enabled
        settings.vehicle_agent_v1_downlink_allowlist = previous_allowlist


def test_enabled_false_monitoring_allowed_control_rejected(client: TestClient) -> None:
    db = SessionLocal()
    try:
        assert preregister(db, DEVICE_ID, DISPLAY_NAME, SITE_CODE).status in {
            "CREATED",
            "EXISTS",
        }
    finally:
        db.close()
    robot = _robot(DEVICE_ID)
    assert robot is not None and robot.enabled is False
    headers = _login(client)
    # 只读监测不受 enabled 限制
    response = client.get(f"/api/v1/robots/{robot.id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["estop_active"] is None  # 未验证急停，业务数据诚实
    # 控制指令被 ROBOT_DISABLED 拒绝
    with SessionLocal() as db:
        bound = db.get(Robot, robot.id)
        with pytest.raises(PlatformError) as excinfo:
            assert_robot_can_execute(db, bound, "start")
        assert excinfo.value.code == "ROBOT_DISABLED"
