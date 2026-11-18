"""G4-Fleet-R2：enrollment activate 的 Transport Profile 完整性 / device token 延迟切换 / 媒体。"""

from __future__ import annotations

import hashlib

import pytest
from app.db.models import EnrollmentToken, Robot, RobotFleetAssignment, Site
from app.db.session import SessionLocal
from app.main import app
from app.modules.enrollment.token_store import issue_token
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

VID = "firebot-vehicle-88"
OLD_DEVICE_TOKEN = "old-device-token-in-use-on-vehicle"
NEW_DEVICE_TOKEN = "new-device-token-from-reregister"
SRT_TOKEN = "c" * 64


def _cred(**over):
    cred = {
        "device_id": VID,
        "profile_id": "firebot_ros1_standard_v1",
        "transport_profile": "u-dao-standard-v1",
        "protocol": "canonical-mqtt-1.3",
        "broker_ref": "u-dao-firebot/mosquitto",
        "mqtt_host": "firebot.youdaozd.com",
        "mqtt_port": 8883,
        "ca_vehicle_path": "certs/mqtt-ca.crt",
        "mqtt_password": "per-device-mqtt-pw",
        "device_token": NEW_DEVICE_TOKEN,
        "media": {
            "kind": "firebot.media-credentials.v1",
            "vehicle_id": VID,
            "srt_host": "firebot.youdaozd.com",
            "srt_port": 8890,
            "srt_publish_token": SRT_TOKEN,
            "srt_publish_passphrase": "platform-shared-pass",
        },
    }
    cred.update(over)
    return cred


@pytest.fixture()
def robot():
    with SessionLocal() as db:
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        r = Robot(vehicle_id=VID, name=VID, site_id=site.id, enabled=False)
        db.add(r)
        db.flush()
        db.add(
            RobotFleetAssignment(
                robot_id=r.id,
                device_token_hash=hashlib.sha256(OLD_DEVICE_TOKEN.encode()).hexdigest(),
            )
        )
        db.commit()
        rid = r.id
    yield rid
    with SessionLocal() as db:
        db.execute(delete(EnrollmentToken).where(EnrollmentToken.device_id == VID))
        db.execute(delete(RobotFleetAssignment).where(RobotFleetAssignment.robot_id == rid))
        db.execute(delete(Robot).where(Robot.id == rid))
        db.commit()


def _issue(cred) -> str:
    with SessionLocal.begin() as db:
        return issue_token(db, VID, cred)


def _hash(rid):
    with SessionLocal() as db:
        return db.get(RobotFleetAssignment, rid).device_token_hash


def test_reregister_does_not_invalidate_device_token_until_activate(robot):
    token = _issue(_cred())
    # 重复 register 后、车辆 activate 前：旧 device token 仍然有效
    assert _hash(robot) == hashlib.sha256(OLD_DEVICE_TOKEN.encode()).hexdigest()
    with TestClient(app) as c:
        r = c.post("/api/v1/enrollment/activate", json={"device_id": VID, "token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transport_profile"] == "u-dao-standard-v1"
    assert body["mqtt_port"] == 8883 and body["protocol"] == "canonical-mqtt-1.3"
    assert body["media"]["srt_publish_token"] == SRT_TOKEN
    assert body["media"]["vehicle_id"] == VID
    assert "media_publish_token" not in r.text  # 主密钥字段永不出现
    # activate 成功同事务切换为新 device token
    assert _hash(robot) == hashlib.sha256(NEW_DEVICE_TOKEN.encode()).hexdigest()
    with SessionLocal() as db:
        row = db.scalar(select(EnrollmentToken).where(EnrollmentToken.device_id == VID))
        assert row.consumed_at is not None
        assert row.credential_json == {}  # 消费即清除，凭证不在 DB 静置


@pytest.mark.parametrize("missing", ["transport_profile", "protocol", "mqtt_host", "mqtt_port"])
def test_incomplete_transport_fail_closed_and_token_not_consumed(robot, missing):
    cred = _cred()
    cred.pop(missing)
    token = _issue(cred)
    with TestClient(app) as c:
        r = c.post("/api/v1/enrollment/activate", json={"device_id": VID, "token": token})
    assert r.status_code == 409, r.text
    assert "8883" not in r.text
    # 回滚：token 未消费、凭证未清除、device token 未切换
    with SessionLocal() as db:
        row = db.scalar(select(EnrollmentToken).where(EnrollmentToken.device_id == VID))
        assert row.consumed_at is None and row.credential_json
    assert _hash(robot) == hashlib.sha256(OLD_DEVICE_TOKEN.encode()).hexdigest()


def test_media_identity_mismatch_rejected(robot):
    cred = _cred()
    cred["media"] = dict(cred["media"], vehicle_id="firebot-vehicle-02")
    token = _issue(cred)
    with TestClient(app) as c:
        r = c.post("/api/v1/enrollment/activate", json={"device_id": VID, "token": token})
    assert r.status_code == 409
    assert _hash(robot) == hashlib.sha256(OLD_DEVICE_TOKEN.encode()).hexdigest()


def test_replay_after_success_rejected(robot):
    token = _issue(_cred())
    body = {"device_id": VID, "token": token}
    with TestClient(app) as c:
        assert c.post("/api/v1/enrollment/activate", json=body).status_code == 200
        assert c.post("/api/v1/enrollment/activate", json=body).status_code == 401
