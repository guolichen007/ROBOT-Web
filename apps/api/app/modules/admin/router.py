from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update

from app.core.audit import write_audit
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.security import hash_password
from app.core.serialization import serialize_model
from app.core.websocket import release_user_leases
from app.db.models import (
    AppSetting,
    AuditLog,
    Permission,
    RefreshSession,
    Role,
    User,
    user_roles,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str
    password: str = Field(min_length=12, max_length=256)
    role_codes: list[str] = Field(default_factory=lambda: ["viewer"])


class SettingInput(BaseModel):
    value: dict


@router.get("/users")
def users(
    db: DbSession, auth: AuthContext = Depends(require_permission("user.manage"))
) -> list[dict]:
    return [serialize_model(x) for x in db.scalars(select(User).order_by(User.username)).all()]


@router.post("/users", status_code=201)
def create_user(
    payload: UserInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
) -> dict:
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(409, "用户名已存在")
    row = User(
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        must_change_password=True,
    )
    db.add(row)
    db.flush()
    roles = db.scalars(select(Role).where(Role.code.in_(payload.role_codes))).all()
    if len(roles) != len(set(payload.role_codes)):
        raise HTTPException(400, "包含未知角色")
    for role in roles:
        db.execute(insert(user_roles).values(user_id=row.id, role_id=role.id))
    write_audit(
        db,
        action="USER_CREATE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=row.id,
        after={"username": row.username, "roles": payload.role_codes},
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.put("/users/{user_id}/roles")
def set_roles(
    user_id: str,
    role_codes: list[str],
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("role.manage")),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    roles = db.scalars(select(Role).where(Role.code.in_(role_codes))).all()
    if len(roles) != len(set(role_codes)):
        raise HTTPException(400, "包含未知角色")
    db.execute(delete(user_roles).where(user_roles.c.user_id == user.id))
    for role in roles:
        db.execute(insert(user_roles).values(user_id=user.id, role_id=role.id))
    write_audit(
        db,
        action="USER_ROLES_UPDATE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=user.id,
        after={"roles": role_codes},
        **request_meta(request),
    )
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()
    release_user_leases(user.id, "PERMISSION_CHANGED")
    return {"user_id": user.id, "roles": role_codes}


@router.get("/roles")
def roles(
    db: DbSession, auth: AuthContext = Depends(require_permission("role.manage"))
) -> list[dict]:
    return [serialize_model(x) for x in db.scalars(select(Role).order_by(Role.code)).all()]


@router.get("/permissions")
def permissions(
    db: DbSession, auth: AuthContext = Depends(require_permission("role.manage"))
) -> list[dict]:
    return [
        serialize_model(x) for x in db.scalars(select(Permission).order_by(Permission.code)).all()
    ]


@router.get("/audit")
def audit(
    db: DbSession, auth: AuthContext = Depends(require_permission("audit.read")), limit: int = 500
) -> list[dict]:
    return [
        serialize_model(x)
        for x in db.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 2000))
        ).all()
    ]


@router.get("/settings")
def settings(
    db: DbSession, auth: AuthContext = Depends(require_permission("settings.manage"))
) -> list[dict]:
    return [
        serialize_model(x) for x in db.scalars(select(AppSetting).order_by(AppSetting.key)).all()
    ]


@router.put("/settings/{key}")
def update_setting(
    key: str,
    payload: SettingInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    row = db.get(AppSetting, key)
    if not row:
        row = AppSetting(key=key, value_json=payload.value)
        db.add(row)
    else:
        row.value_json = payload.value
    row.updated_by = auth.user.id
    write_audit(
        db,
        action="SETTING_UPDATE",
        resource_type="SETTING",
        user_id=auth.user.id,
        resource_id=key,
        after=payload.value,
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)
