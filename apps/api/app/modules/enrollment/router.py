"""设备 enrollment API：一次性 token 验证 + per-device credential 签发。

安全边界：
- token 一次性消费（DB 事务化）：重放拒绝、过期拒绝、DEVICE_ID 绑定、并发仅一次成功。
- credential 由 fleet-register 签发生成（per-device），存 DB credential_json，消费后清除。
- 绝不返回 fleet 共用密码。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.session import SessionLocal
from app.modules.enrollment.token_store import consume_token

router = APIRouter(prefix="/api/v1/enrollment", tags=["enrollment"])

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ActivateRequest(BaseModel):
    device_id: str
    token: str


class ActivateResponse(BaseModel):
    device_id: str
    profile_id: str
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

    return ActivateResponse(
        device_id=device_id,
        profile_id=cred.get("profile_id", "firebot_ros1_standard_v1"),
        mqtt_host=cred.get("mqtt_host", ""),
        mqtt_port=int(cred.get("mqtt_port", 8883)),
        mqtt_username=device_id,
        mqtt_password=cred["mqtt_password"],
        device_token=cred.get("device_token", ""),
        ca_cert=cred.get("ca_cert", "/etc/firebot/production-ca.crt"),
        site_code=cred.get("site_code", ""),
        map_code=cred.get("map_code", ""),
        map_version=cred.get("map_version", ""),
        map_checksum=cred.get("map_checksum", ""),
    )
