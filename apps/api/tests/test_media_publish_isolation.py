"""G3 媒体发布鉴权隔离反证（/api/v1/media/authorize）。

旧实现：全局 MEDIA_PUBLISH_TOKEN（token 字段）可发布**任意已登记流**，包括真实车辆流
（firebot-vehicle-01、R002）；RTSP 路径下 user 为空即走全局分支。
新语义：
- 全局合成 token 只能发布 source_ref 明确登记为 SYNTHETIC_TEST 的流；
- 真实车辆流只接受车辆绑定的 per-vehicle 身份（user=vehicle_id + HMAC 派生口令）；
- 车辆凭证不能跨车（R001 → R002 / firebot-vehicle-01 一律拒绝）；
- 播放票据过期 / 跨流 / 跨车辆拒绝；日志不出现 token / passphrase / 带密钥 URL。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.core.config import get_settings
from app.db.models import Robot, Site, StreamRegistry, User
from app.db.session import SessionLocal
from app.main import app
from app.modules.media.router import encode_media_ticket, vehicle_srt_publish_token
from fastapi.testclient import TestClient
from sqlalchemy import select

REAL_VEHICLE = "firebot-vehicle-01"
SIM_STREAM = "firebot-sim-media-01-roof_rgb"


@pytest.fixture(scope="module", autouse=True)
def synthetic_assets():
    """仅本测试：把 firebot-sim-media-01 配置为合成媒体资产（生产默认空）。"""
    settings = get_settings()
    previous = settings.synthetic_media_vehicle_ids
    settings.synthetic_media_vehicle_ids = "firebot-sim-media-01"
    yield
    settings.synthetic_media_vehicle_ids = previous


@pytest.fixture(scope="module", autouse=True)
def streams(synthetic_assets):
    """测试库：真实车辆 001 的 RGB 流（非合成）+ 显式登记的合成测试流。"""
    from app.modules.media.router import SYNTHETIC_TEST_SOURCE

    with SessionLocal() as db:
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        created = []
        for vehicle, name in ((REAL_VEHICLE, "firebot-001"), ("firebot-sim-media-01", "sim")):
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle))
            if robot is None:
                robot = Robot(vehicle_id=vehicle, name=name, site_id=site.id, enabled=False)
                db.add(robot)
                db.flush()
                created.append(robot.id)
        real = db.scalar(select(Robot).where(Robot.vehicle_id == REAL_VEHICLE))
        sim = db.scalar(select(Robot).where(Robot.vehicle_id == "firebot-sim-media-01"))
        for stream_id, robot, source in (
            (f"{REAL_VEHICLE}-roof_rgb", real, None),
            (SIM_STREAM, sim, SYNTHETIC_TEST_SOURCE),
        ):
            row = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id))
            if row is None:
                db.add(
                    StreamRegistry(
                        stream_id=stream_id,
                        robot_id=robot.id,
                        camera_type="roof_rgb",
                        source_ref=source,
                        state="OFFLINE",
                    )
                )
        db.commit()
    yield
    # 共享测试库清理：只删除本测试创建的行（不触碰 seed 或其他套件资产）
    from sqlalchemy import delete

    with SessionLocal() as db:
        db.execute(
            delete(StreamRegistry).where(
                StreamRegistry.stream_id.in_([f"{REAL_VEHICLE}-roof_rgb", SIM_STREAM])
            )
        )
        if created:
            from app.db.models import Base

            for table in reversed(Base.metadata.sorted_tables):
                if "robot_id" in table.c and table.name != "robots":
                    db.execute(table.delete().where(table.c.robot_id.in_(created)))
            db.execute(delete(Robot).where(Robot.id.in_(created)))
        db.commit()


def publish(client, path, *, protocol="rtsp", user="", password="", token=""):
    return client.post(
        "/api/v1/media/authorize",
        json={
            "action": "publish",
            "path": path,
            "protocol": protocol,
            "user": user,
            "password": password,
            "token": token,
        },
    )


def global_token() -> str:
    return get_settings().effective_media_publish_token


@pytest.mark.parametrize("path", ["R001-roof_rgb", "R002-roof_rgb", f"{REAL_VEHICLE}-roof_rgb"])
@pytest.mark.parametrize("protocol", ["rtsp", "srt", "webrtc", ""])
def test_global_token_cannot_publish_real_vehicle_streams(path, protocol):
    with TestClient(app) as client:
        assert publish(client, path, protocol=protocol, token=global_token()).status_code in (
            401,
            403,
        )
        # 全局 token 放进 password 字段（user 为空）同样拒绝
        assert publish(client, path, protocol=protocol, password=global_token()).status_code in (
            401,
            403,
        )


def test_synthetic_stream_accepts_only_its_derived_token():
    from app.modules.media.router import synthetic_media_publish_token

    with TestClient(app) as client:
        assert (
            publish(client, SIM_STREAM, token=synthetic_media_publish_token(SIM_STREAM)).status_code
            == 200
        )
        # G3-R1：原始全局主密钥不再是发布凭据（不应出现在任何发布进程参数中）
        assert publish(client, SIM_STREAM, token=global_token()).status_code == 401
        # 他流派生 token 不能跨流使用
        other = synthetic_media_publish_token("firebot-sim-media-01-roof_thermal")
        assert publish(client, SIM_STREAM, token=other).status_code == 401
        assert publish(client, SIM_STREAM, token="WRONG").status_code == 401
        assert publish(client, SIM_STREAM, token="").status_code == 401


@pytest.mark.parametrize("vehicle", ["R001", "R002", REAL_VEHICLE])
def test_mislabelled_real_vehicle_stream_not_publishable_with_synthetic_token(vehicle):
    """真实车辆流即使被误标 source_ref=SYNTHETIC_TEST（甚至被误配进合成资产名单），
    也不允许任何合成/全局 token 发布。"""
    from app.modules.media.router import SYNTHETIC_TEST_SOURCE, synthetic_media_publish_token

    stream_id = f"{vehicle}-roof_rgb"
    settings = get_settings()
    previous = settings.synthetic_media_vehicle_ids
    with SessionLocal() as db:
        row = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id))
        original = row.source_ref
        row.source_ref = SYNTHETIC_TEST_SOURCE
        db.commit()
    try:
        with TestClient(app) as client:
            for misconfig in ("", f"firebot-sim-media-01,{vehicle}"):
                settings.synthetic_media_vehicle_ids = misconfig
                for token in (global_token(), synthetic_media_publish_token(stream_id)):
                    assert publish(client, stream_id, token=token).status_code in (401, 403)
    finally:
        settings.synthetic_media_vehicle_ids = previous
        with SessionLocal() as db:
            row = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id))
            row.source_ref = original
            db.commit()


def test_vehicle_identity_bound_and_not_cross_vehicle():
    r001 = vehicle_srt_publish_token("R001")
    v01 = vehicle_srt_publish_token(REAL_VEHICLE)
    with TestClient(app) as client:
        for protocol in ("srt", "rtsp"):
            ok = publish(client, "R001-roof_rgb", protocol=protocol, user="R001", password=r001)
            assert ok.status_code == 200, (protocol, ok.text)
            ok = publish(
                client,
                f"{REAL_VEHICLE}-roof_rgb",
                protocol=protocol,
                user=REAL_VEHICLE,
                password=v01,
            )
            assert ok.status_code == 200, (protocol, ok.text)
            for path in ("R002-roof_rgb", f"{REAL_VEHICLE}-roof_rgb"):
                cross = publish(client, path, protocol=protocol, user="R001", password=r001)
                assert cross.status_code in (401, 403), (protocol, path)
            # 冒名：user 写成目标车，但口令是 R001 的
            spoof = publish(client, "R002-roof_rgb", protocol=protocol, user="R002", password=r001)
            assert spoof.status_code == 401
        # 车辆凭证不得发布合成流（合成流只接受全局合成 token）
        assert publish(
            client, SIM_STREAM, protocol="srt", user="R001", password=r001
        ).status_code in (
            401,
            403,
        )


def test_playback_ticket_expired_cross_stream_cross_vehicle():
    with TestClient(app) as client, SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        r001 = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        r002 = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        good = encode_media_ticket(
            user_id=admin.id, robot_id=r001.id, camera="roof_rgb", stream_id="R001-roof_rgb"
        )
        read = {"action": "read", "protocol": "webrtc"}
        ok = client.post(
            "/api/v1/media/authorize", json={**read, "path": "R001-roof_rgb", "token": good}
        )
        assert ok.status_code == 200
        # 跨流 / 跨车辆
        for path in ("R001-roof_thermal", "R002-roof_rgb"):
            denied = client.post(
                "/api/v1/media/authorize", json={**read, "path": path, "token": good}
            )
            assert denied.status_code == 403, path
        forged = encode_media_ticket(
            user_id=admin.id, robot_id=r001.id, camera="roof_rgb", stream_id="R002-roof_rgb"
        )
        denied = client.post(
            "/api/v1/media/authorize", json={**read, "path": "R002-roof_rgb", "token": forged}
        )
        assert denied.status_code == 403 and r002.id != r001.id
        now = datetime.now(UTC)
        expired = jwt.encode(
            {
                "type": "media",
                "sub": admin.id,
                "robot_id": r001.id,
                "camera": "roof_rgb",
                "stream_id": "R001-roof_rgb",
                "iat": now - timedelta(minutes=10),
                "exp": now - timedelta(minutes=5),
                "jti": "x",
            },
            get_settings().effective_jwt_secret,
            algorithm="HS256",
        )
        late = client.post(
            "/api/v1/media/authorize", json={**read, "path": "R001-roof_rgb", "token": expired}
        )
        assert late.status_code == 401
        bogus = client.post(
            "/api/v1/media/authorize", json={**read, "path": "R001-roof_rgb", "token": "x.y.z"}
        )
        assert bogus.status_code == 401


def test_no_credentials_in_logs(caplog):
    secret = vehicle_srt_publish_token("R001")
    url = f"srt://mediamtx:8890?streamid=publish:R002-roof_rgb:R001:{secret}&passphrase=PASSPHRASE-XYZ"
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        publish(client, "R002-roof_rgb", protocol="srt", user="R001", password=secret)
        publish(client, "R001-roof_rgb", token=global_token())
        client.post(
            "/api/v1/media/authorize", json={"action": "publish", "path": url, "token": secret}
        )
    text = caplog.text
    for value in (secret, global_token(), "PASSPHRASE-XYZ", url):
        assert value not in text
    assert "media publish denied" in text  # 拒绝有审计日志（不含凭据）


@pytest.mark.parametrize("action", ["ptz", "camera_ptz", "nuc", "thermal_mode", "set_measure_mode"])
def test_no_camera_hardware_control_commands(action):
    """G3：相机 PTZ / NUC / 测温模式等硬件控制不在 V1 命令集，API 必须拒绝（不得伪造执行成功）。"""
    from services.vehicle_agent_v1 import COMMANDS

    assert action not in COMMANDS
    from app.core.security import hash_password
    from app.db.models import Role, user_roles
    from sqlalchemy import insert

    with SessionLocal() as db:  # 测试库专用管理员（不触碰 bootstrap admin）
        user = db.scalar(select(User).where(User.username == "g3-media-test-admin"))
        if user is None:
            user = User(
                username="g3-media-test-admin",
                password_hash=hash_password("G3-Media-Test-Only-2026!"),
                display_name="g3 test",
                must_change_password=False,
            )
            db.add(user)
            db.flush()
            role = db.scalar(select(Role).where(Role.code == "super_admin"))
            db.execute(insert(user_roles).values(user_id=user.id, role_id=role.id))
            db.commit()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "g3-media-test-admin", "password": "G3-Media-Test-Only-2026!"},
        )
        assert login.status_code == 200, login.text
        response = client.post(
            f"/api/v1/robots/R001/agent/commands/{action}",
            json={"params": {}},
            headers={
                "Authorization": "Bearer " + login.json()["access_token"],
                "Idempotency-Key": "g3-" + action,
            },
        )
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "UNKNOWN_COMMAND"
