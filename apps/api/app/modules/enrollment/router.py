"""设备 enrollment API：一次性 token 验证 + per-device credential 签发。

安全边界：
- token 一次性消费（DB 事务化）：重放拒绝、过期拒绝、DEVICE_ID 绑定、并发仅一次成功。
- credential 由 fleet-register 经批准 Transport Profile 签发（per-device），存 DB credential_json，
  消费后清除。
- G4-Fleet-R2：接入参数（host/port/protocol/transport_profile/CA）必须完整来自签发记录；缺任何
  一项 → 409 fail-closed（不再默认 8883 / 空 host）。
- device token：只在本次 activate 成功的同一事务里替换 assignment.device_token_hash——
  服务器重复 register 不会让车上正在使用的 device token 提前失效。
- 媒体凭证（单车 SRT token + 平台共享 passphrase + endpoint）随 activate 一次性下发；
  主密钥永不下发。
- 绝不返回 fleet 共用密码。
"""

from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Robot, RobotFleetAssignment
from app.db.session import SessionLocal
from app.modules.enrollment.token_store import consume_token

router = APIRouter(prefix="/api/v1/enrollment", tags=["enrollment"])

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRANSPORT_FIELDS = ("transport_profile", "protocol", "mqtt_host", "mqtt_port")


class ActivateRequest(BaseModel):
    device_id: str
    token: str


class MediaCredential(BaseModel):
    kind: str
    vehicle_id: str
    srt_host: str
    srt_port: int
    srt_publish_token: str
    srt_publish_passphrase: str


class ActivateResponse(BaseModel):
    device_id: str
    profile_id: str
    transport_profile: str
    protocol: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    device_token: str
    ca_cert: str
    site_code: str
    map_code: str
    map_version: str
    map_checksum: str
    media: MediaCredential | None = None


def _transport_complete(cred: dict) -> bool:
    if any(cred.get(k) in (None, "") for k in TRANSPORT_FIELDS):
        return False
    try:
        return 1 <= int(cred["mqtt_port"]) <= 65535
    except (TypeError, ValueError):
        return False


@router.post("/activate", response_model=ActivateResponse)
def activate(req: ActivateRequest) -> ActivateResponse:
    device_id = req.device_id.strip()
    if not DEVICE_ID_RE.match(device_id):
        raise HTTPException(status_code=400, detail="DEVICE_ID 格式非法")

    with SessionLocal.begin() as db:
        cred = consume_token(db, device_id, req.token)
        if cred is None:
            # 重放 / 过期 / 不匹配 / 已消费，统一 401（不泄露具体原因）
            raise HTTPException(status_code=401, detail="enrollment token 无效或已使用")
        if not _transport_complete(cred) or not cred.get("mqtt_password"):
            # 旧格式（无 Transport Profile）签发记录：拒绝并回滚（token 不被消费）
            raise HTTPException(
                status_code=409,
                detail="签发记录缺少批准的 Transport Profile 参数；请服务器重新 fleet register",
            )
        media = cred.get("media")
        if media is not None and media.get("vehicle_id") != device_id:
            raise HTTPException(status_code=409, detail="媒体凭证身份与设备不一致")
        device_token = cred.get("device_token", "")
        if device_token:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == device_id))
            if robot is None:
                raise HTTPException(status_code=409, detail="设备未预登记")
            assignment = db.get(RobotFleetAssignment, robot.id)
            if assignment is None:
                assignment = RobotFleetAssignment(robot_id=robot.id)
                db.add(assignment)
            assignment.device_token_hash = hashlib.sha256(device_token.encode("utf-8")).hexdigest()

    return ActivateResponse(
        device_id=device_id,
        profile_id=cred.get("profile_id", "firebot_ros1_standard_v1"),
        transport_profile=cred["transport_profile"],
        protocol=cred["protocol"],
        mqtt_host=cred["mqtt_host"],
        mqtt_port=int(cred["mqtt_port"]),
        mqtt_username=device_id,
        mqtt_password=cred["mqtt_password"],
        device_token=device_token,
        # MQTT 信任锚（车端 Edge Home certs/mqtt-ca.crt）；HTTPS 走系统 CA，不复用本字段
        ca_cert=cred.get("ca_vehicle_path") or cred.get("ca_cert") or "/etc/firebot/mqtt-ca.crt",
        site_code=cred.get("site_code", ""),
        map_code=cred.get("map_code", ""),
        map_version=cred.get("map_version", ""),
        map_checksum=cred.get("map_checksum", ""),
        media=MediaCredential(**media) if media else None,
    )
