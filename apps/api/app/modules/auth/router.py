from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import CurrentAuth, DbSession, load_permissions, request_meta
from app.core.events import get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    csrf_digest,
    decode_refresh_token,
    hash_password,
    hash_token,
    random_token,
    verify_password,
)
from app.core.websocket import release_user_leases
from app.db.models import RefreshSession, Role, User, user_roles

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=256)


def user_payload(db: Session, user: User) -> dict:
    permissions = sorted(load_permissions(db, user.id))
    roles = list(
        db.execute(
            select(Role.code)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(user_roles.c.user_id == user.id)
        ).scalars()
    )
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "must_change_password": user.must_change_password,
        "roles": roles,
        "permissions": permissions,
    }


def set_refresh_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.refresh_token_days * 86_400
    response.set_cookie(
        "refresh_token",
        refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=max_age,
        path="/api/v1/auth",
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=max_age,
        path="/",
    )


def issue_session(
    db: Session, response: Response, user: User, family_id: str | None = None
) -> dict:
    settings = get_settings()
    family = family_id or str(uuid4())
    refresh_token = create_refresh_token(user.id, family)
    csrf_token = random_token(24)
    session = RefreshSession(
        user_id=user.id,
        family_id=family,
        token_hash=hash_token(refresh_token),
        csrf_hash=csrf_digest(csrf_token),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(session)
    db.flush()
    set_refresh_cookies(response, refresh_token, csrf_token)
    permissions = sorted(load_permissions(db, user.id))
    return {
        "access_token": create_access_token(user.id, permissions),
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
        "user": user_payload(db, user),
        "refresh_session_id": session.id,
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: DbSession) -> dict:
    settings = get_settings()
    redis = get_redis()
    ip = request.client.host if request.client else "unknown"
    key = f"login-fail:{ip}:{payload.username.lower()}"
    failures = int(redis.get(key) or 0)
    if failures >= settings.login_failure_limit:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
    user = db.scalar(select(User).where(User.username == payload.username))
    now = datetime.now(UTC)
    if user and user.locked_until and user.locked_until > now:
        raise HTTPException(status_code=423, detail="账号暂时锁定")
    if not user or not verify_password(payload.password, user.password_hash):
        failures = redis.incr(key)
        redis.expire(key, settings.login_lock_seconds)
        if user:
            user.failed_attempts += 1
            if failures >= settings.login_failure_limit:
                user.locked_until = now + timedelta(seconds=settings.login_lock_seconds)
            write_audit(
                db,
                action="AUTH_LOGIN",
                resource_type="USER",
                user_id=user.id,
                resource_id=user.id,
                result="FAILED",
                **request_meta(request),
            )
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if user.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="账号不可用")
    redis.delete(key)
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    result = issue_session(db, response, user)
    result.pop("refresh_session_id", None)
    write_audit(
        db,
        action="AUTH_LOGIN",
        resource_type="USER",
        user_id=user.id,
        resource_id=user.id,
        **request_meta(request),
    )
    db.commit()
    return result


def validate_csrf(
    session: RefreshSession, csrf_header: str | None, csrf_cookie: str | None
) -> None:
    if not csrf_header or not csrf_cookie or csrf_header != csrf_cookie:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    if csrf_digest(csrf_header) != session.csrf_hash:
        raise HTTPException(status_code=403, detail="CSRF 会话不匹配")


@router.post("/refresh")
def refresh(
    response: Response,
    db: DbSession,
    refresh_token: str | None = Cookie(default=None),
    csrf_token: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> dict:
    if not refresh_token:
        raise HTTPException(status_code=401, detail="缺少刷新会话")
    try:
        payload = decode_refresh_token(refresh_token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="刷新会话无效") from exc
    session = db.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == hash_token(refresh_token))
    )
    if not session or session.revoked_at or session.expires_at <= datetime.now(UTC):
        if payload.get("family_id"):
            db.execute(
                update(RefreshSession)
                .where(RefreshSession.family_id == payload["family_id"])
                .values(revoked_at=datetime.now(UTC))
            )
            db.commit()
        raise HTTPException(status_code=401, detail="刷新会话已撤销或过期")
    validate_csrf(session, x_csrf_token, csrf_token)
    user = db.get(User, session.user_id)
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=401, detail="用户不可用")
    session.revoked_at = datetime.now(UTC)
    result = issue_session(db, response, user, family_id=session.family_id)
    session.replaced_by = result["refresh_session_id"]
    result.pop("refresh_session_id", None)
    db.commit()
    return result


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    db: DbSession,
    refresh_token: str | None = Cookie(default=None),
    csrf_token: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> Response:
    user_id: str | None = None
    if refresh_token:
        session = db.scalar(
            select(RefreshSession).where(RefreshSession.token_hash == hash_token(refresh_token))
        )
        if session:
            user_id = session.user_id
            validate_csrf(session, x_csrf_token, csrf_token)
            db.execute(
                update(RefreshSession)
                .where(RefreshSession.family_id == session.family_id)
                .values(revoked_at=datetime.now(UTC))
            )
            db.commit()
    if user_id:
        release_user_leases(user_id, "USER_LOGOUT")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    response.delete_cookie("csrf_token", path="/")
    response.status_code = 204
    return response


@router.get("/me")
def me(auth: CurrentAuth, db: DbSession) -> dict:
    return user_payload(db, auth.user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest, request: Request, auth: CurrentAuth, db: DbSession
) -> dict:
    if not verify_password(payload.current_password, auth.user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    auth.user.password_hash = hash_password(payload.new_password)
    auth.user.must_change_password = False
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == auth.user.id)
        .values(revoked_at=datetime.now(UTC))
    )
    write_audit(
        db,
        action="AUTH_CHANGE_PASSWORD",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=auth.user.id,
        **request_meta(request),
    )
    db.commit()
    release_user_leases(auth.user.id, "PASSWORD_CHANGED")
    return {"changed": True, "reauthenticate": True}


@router.post("/ws-ticket")
def ws_ticket(request: Request, auth: CurrentAuth) -> dict:
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().origins:
        raise HTTPException(status_code=403, detail="WebSocket Origin 不允许")
    ticket = random_token(32)
    get_redis().setex(
        f"ws-ticket:{ticket}",
        get_settings().ws_ticket_seconds,
        json.dumps({"user_id": auth.user.id, "permissions": sorted(auth.permissions)}),
    )
    return {"ticket": ticket, "expires_in": get_settings().ws_ticket_seconds}
