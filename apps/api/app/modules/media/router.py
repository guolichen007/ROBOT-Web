from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.dependencies import AuthContext, DbSession, load_permissions, require_permission
from app.core.errors import PlatformError
from app.core.serialization import serialize_model
from app.db.models import Robot, StreamRegistry, User

router = APIRouter(prefix="/api/v1/media", tags=["media"])


class TicketRequest(BaseModel):
    stream_id: str


class MediaAuthRequest(BaseModel):
    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str
    path: str = ""
    protocol: str = ""
    id: str = ""
    query: str = ""
    userAgent: str = ""


def vehicle_srt_publish_token(vehicle_id: str) -> str:
    """per-vehicle SRT 发布 token：HMAC-SHA256(master, "vehicle-srt:"+vehicle_id)。

    确定性派生（不存库、不读运行时目录）；车辆 token 泄露无法推出他车 token；
    校验使用 hmac.compare_digest。
    """
    import hashlib
    import hmac as hmac_mod

    master = get_settings().effective_media_publish_token
    return hmac_mod.new(
        master.encode("utf-8"),
        f"vehicle-srt:{vehicle_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def encode_media_ticket(*, user_id: str, robot_id: str, camera: str, stream_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "type": "media",
            "sub": user_id,
            "robot_id": robot_id,
            "camera": camera,
            "stream_id": stream_id,
            "iat": now,
            "exp": now + timedelta(seconds=settings.media_ticket_seconds),
            "jti": str(uuid4()),
        },
        settings.effective_jwt_secret,
        algorithm="HS256",
    )


def decode_media_ticket(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            get_settings().effective_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "robot_id", "camera", "stream_id"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise PlatformError("AUTH_REQUIRED", "媒体票据已过期", status_code=401) from exc
    except jwt.InvalidTokenError as exc:
        raise PlatformError("AUTH_REQUIRED", "媒体票据无效", status_code=401) from exc
    if payload.get("type") != "media":
        raise PlatformError("AUTH_REQUIRED", "媒体票据类型无效", status_code=401)
    return payload


@router.get("/streams")
def streams(
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
    robot_id: str | None = None,
) -> list[dict]:
    query = select(StreamRegistry).order_by(StreamRegistry.camera_type)
    if robot_id:
        robot = db.scalar(
            select(Robot).where(or_(Robot.id == robot_id, Robot.vehicle_id == robot_id))
        )
        if not robot:
            raise PlatformError("RESOURCE_NOT_FOUND", "机器人不存在", status_code=404)
        query = query.where(StreamRegistry.robot_id == robot.id)
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.post("/tickets")
def media_ticket(
    payload: TicketRequest,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    stream = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == payload.stream_id))
    if not stream:
        raise PlatformError("RESOURCE_NOT_FOUND", "媒体流不存在", status_code=404)
    token = encode_media_ticket(
        user_id=auth.user.id,
        robot_id=stream.robot_id,
        camera=stream.camera_type,
        stream_id=stream.stream_id,
    )
    return {
        "ticket": token,
        "expires_in": get_settings().media_ticket_seconds,
        "stream_id": stream.stream_id,
        "robot_id": stream.robot_id,
        "camera": stream.camera_type,
        "playback_url": f"/media/{stream.stream_id}/whep",
        "authorization_scheme": "Bearer",
    }


@router.post("/authorize", include_in_schema=False)
def authorize_media(payload: MediaAuthRequest, db: DbSession) -> dict:
    if payload.action == "publish":
        stream = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == payload.path))
        if not stream:
            raise PlatformError("PERMISSION_DENIED", "媒体发布路径未登记", status_code=403)
        robot = db.get(Robot, stream.robot_id)
        if payload.protocol == "srt" and payload.user:
            # per-vehicle SRT 发布：user 必须是流所属 canonical vehicle_id，
            # password == HMAC 派生 token（R001 token 不得用于 R002 流）。
            if (
                robot
                and payload.user == robot.vehicle_id
                and hmac.compare_digest(
                    payload.password, vehicle_srt_publish_token(robot.vehicle_id)
                )
            ):
                return {"authorized": True, "action": payload.action, "stream_id": stream.stream_id}
            raise PlatformError("AUTH_REQUIRED", "媒体发布凭据无效", status_code=401)
        # bootstrap/synthetic 全局 token（明确作用域：仅合成测试源；真实多车使用 per-vehicle）。
        if payload.token != get_settings().effective_media_publish_token:
            raise PlatformError("AUTH_REQUIRED", "媒体发布凭据无效", status_code=401)
        return {"authorized": True, "action": payload.action, "stream_id": stream.stream_id}
    if payload.action not in {"read", "playback"}:
        raise PlatformError("PERMISSION_DENIED", "媒体动作未授权", status_code=403)
    # Browser playback tickets are accepted only through the Authorization
    # header propagated by MediaMTX. URL query tokens leak through history,
    # reverse-proxy access logs and referrers and are intentionally unsupported.
    claims = decode_media_ticket(payload.token)
    user = db.get(User, claims["sub"])
    if not user or user.status != "ACTIVE" or "robot.read" not in load_permissions(db, user.id):
        raise PlatformError("PERMISSION_DENIED", "媒体票据所属用户已无查看权限", status_code=403)
    if claims["stream_id"] != payload.path:
        raise PlatformError("PERMISSION_DENIED", "媒体票据与流路径不匹配", status_code=403)
    stream = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == payload.path))
    if (
        not stream
        or stream.robot_id != claims["robot_id"]
        or stream.camera_type != claims["camera"]
    ):
        raise PlatformError("PERMISSION_DENIED", "媒体票据绑定对象不匹配", status_code=403)
    return {"authorized": True, "action": payload.action, "stream_id": stream.stream_id}
