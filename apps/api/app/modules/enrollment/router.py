"""设备 enrollment API：一次性 token 验证 + per-device credential 签发。

安全边界：
- 只监听 Tailnet 地址（部署时 Nginx 只对 Tailnet 暴露本路径）。
- token 一次性消费：重放拒绝、过期拒绝、DEVICE_ID 绑定。
- 绝不返回 fleet 共用密码。
"""

from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.modules.enrollment.token_store import TokenStore

router = APIRouter(prefix="/api/v1/enrollment", tags=["enrollment"])

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# pending credential 文件目录：fleet-register.sh 写入 {device_id}.cred（0600），本 API 读取后删除。
CREDENTIAL_DIR = os.environ.get("FIREBOT_ENROLLMENT_CREDENTIAL_DIR", "/opt/firebot/enrollment")


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

    store = TokenStore(CREDENTIAL_DIR)
    if not store.consume(device_id, req.token):
        # 重放 / 过期 / 不匹配 / 已消费，统一 401（不泄露具体原因）
        raise HTTPException(status_code=401, detail="enrollment token 无效或已使用")

    cred_path = os.path.join(CREDENTIAL_DIR, f"{device_id}.cred")
    try:
        with open(cred_path, encoding="utf-8") as f:
            cred = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail="credential 未就绪") from exc

    # 读取后删除 pending credential（单次交付）
    try:
        os.remove(cred_path)
    except OSError:
        pass

    return ActivateResponse(
        device_id=device_id,
        profile_id=cred.get("profile_id", "firebot_ros1_standard_v1"),
        mqtt_host=cred.get("mqtt_host", ""),
        mqtt_port=int(cred.get("mqtt_port", 8883)),
        mqtt_username=device_id,
        mqtt_password=cred["mqtt_password"],
        ca_cert=cred.get("ca_cert", "/etc/firebot/production-ca.crt"),
        site_code=cred.get("site_code", ""),
        map_code=cred.get("map_code", ""),
        map_version=cred.get("map_version", ""),
        map_checksum=cred.get("map_checksum", ""),
    )
