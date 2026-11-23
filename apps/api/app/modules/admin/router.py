from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.dependencies import (
    AuthContext,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.errors import PlatformError
from app.core.idempotency import lookup, store
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


# ---------------------------------------------------------------------------
# WEB-R1-MIN 账号管理：固定六类角色，单组织单站点。
# 角色授予规则（服务端强制，前端只做展示）：
#   - super_admin 只能由 super_admin 授予 / 撤销；
#   - administrator 需要授予者同时持有 user.manage + role.manage；
#   - 任何人不得修改自己的角色、状态（防止自我提权 / 自我锁死）；
#   - 系统必须始终保留至少一个 ACTIVE 的管理员（super_admin 或 administrator）。
# 删除 = 逻辑停用（status=DISABLED），保留历史关联（任务、审计、命令 created_by）。
# 权限 / 状态 / 密码变化 → token_version+1、撤销刷新会话、释放手控租约。
# ---------------------------------------------------------------------------

ROLE_CODES = ("super_admin", "administrator", "dispatcher", "operator", "viewer", "auditor")
ADMIN_ROLES = {"super_admin", "administrator"}
USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,64}$"
USER_STATUSES = {"ACTIVE", "DISABLED"}


def _validate_password(value: str) -> str:
    if len(value) < 12:
        raise ValueError("密码至少 12 位")
    classes = sum(
        [
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(not c.isalnum() for c in value),
        ]
    )
    if classes < 3:
        raise ValueError("密码需包含大写、小写、数字、符号中的至少三类")
    return value


class UserInput(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=256)
    role_codes: list[str] = Field(default_factory=lambda: ["viewer"], min_length=1, max_length=6)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        return _validate_password(value)


class SettingInput(BaseModel):
    value: dict


class UserProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)


class RoleAssignInput(BaseModel):
    role_codes: list[str] = Field(min_length=1, max_length=6)


class UserStatusInput(BaseModel):
    status: str = Field(pattern=r"^(ACTIVE|DISABLED)$")
    reason: str = Field(default="", max_length=500)


class PasswordResetInput(BaseModel):
    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        return _validate_password(value)


def user_roles_of(db: Session, user_id: str) -> list[str]:
    return sorted(
        db.scalars(
            select(Role.code)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(user_roles.c.user_id == user_id)
        ).all()
    )


def public_user(db: Session, user: User) -> dict:
    """账号对外视图：绝不包含 password_hash / token_version。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "status": user.status,
        "must_change_password": user.must_change_password,
        "roles": user_roles_of(db, user.id),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "locked_until": user.locked_until.isoformat() if user.locked_until else None,
        "deactivated_at": user.deactivated_at.isoformat() if user.deactivated_at else None,
        "deactivated_by": user.deactivated_by,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _actor_roles(db: Session, auth: AuthContext) -> set[str]:
    return set(user_roles_of(db, auth.user.id))


def assert_can_grant(db: Session, auth: AuthContext, role_codes: set[str]) -> None:
    unknown = role_codes - set(ROLE_CODES)
    if unknown:
        raise PlatformError(
            "ROLE_UNKNOWN", "包含未知角色", status_code=400, details={"roles": sorted(unknown)}
        )
    actor = _actor_roles(db, auth)
    if "super_admin" in role_codes and "super_admin" not in actor:
        raise PlatformError(
            "ROLE_GRANT_FORBIDDEN",
            "只有超级管理员可以授予超级管理员角色",
            status_code=403,
            details={"role": "super_admin"},
        )
    if "administrator" in role_codes and not {"user.manage", "role.manage"} <= auth.permissions:
        raise PlatformError(
            "ROLE_GRANT_FORBIDDEN",
            "授予管理员角色需要同时具备用户管理和角色管理权限",
            status_code=403,
            details={"role": "administrator"},
        )


def _assert_can_manage_target(db: Session, auth: AuthContext, target: User) -> None:
    if "super_admin" in user_roles_of(db, target.id) and "super_admin" not in _actor_roles(
        db, auth
    ):
        raise PlatformError(
            "USER_MANAGE_FORBIDDEN", "只有超级管理员可以管理超级管理员账号", status_code=403
        )


def active_admin_count(db: Session, exclude_user_id: str | None = None) -> int:
    query = (
        select(func.count(func.distinct(User.id)))
        .join(user_roles, user_roles.c.user_id == User.id)
        .join(Role, Role.id == user_roles.c.role_id)
        .where(User.status == "ACTIVE", Role.code.in_(ADMIN_ROLES))
    )
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    return int(db.scalar(query) or 0)


def _lock_users(db: Session) -> None:
    # 串行化“最后管理员”判断，避免两个管理员同时互相停用。
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(User.id).with_for_update())


def invalidate_sessions(db: Session, user: User) -> None:
    """权限/状态/密码变化：令旧访问令牌、刷新会话、WS、媒体票据全部失效。"""
    user.token_version = int(user.token_version or 0) + 1
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def _get_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if not user:
        raise PlatformError("RESOURCE_NOT_FOUND", "用户不存在", status_code=404)
    return user


@router.get("/users")
def users(
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
    q: str | None = None,
    status: str | None = None,
    role: str | None = None,
) -> list[dict]:
    query = select(User).order_by(User.username)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(User.username.ilike(like) | User.display_name.ilike(like))
    if status:
        query = query.where(User.status == status)
    rows = [public_user(db, x) for x in db.scalars(query).all()]
    if role:
        rows = [row for row in rows if role in row["roles"]]
    return rows


@router.get("/users/{user_id}")
def user_detail(
    user_id: str, db: DbSession, auth: AuthContext = Depends(require_permission("user.manage"))
) -> dict:
    return public_user(db, _get_user(db, user_id))


@router.post("/users", status_code=201)
def create_user(
    payload: UserInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    body = payload.model_dump(exclude={"password"})
    if idempotency_key:
        cached = lookup(
            db, actor_id=auth.user.id, endpoint="/admin/users", key=idempotency_key, payload=body
        )
        if cached:
            return cached.response_json
    role_codes = set(payload.role_codes)
    if role_codes and not role_codes <= {"viewer"} and "role.manage" not in auth.permissions:
        raise PlatformError(
            "ROLE_GRANT_FORBIDDEN", "缺少角色管理权限，只能创建只读用户", status_code=403
        )
    assert_can_grant(db, auth, role_codes)
    if db.scalar(select(User).where(func.lower(User.username) == payload.username.lower())):
        raise PlatformError("USERNAME_EXISTS", "用户名已存在", status_code=409)
    row = User(
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        must_change_password=True,
        status="ACTIVE",
        token_version=0,
    )
    db.add(row)
    db.flush()
    roles = db.scalars(select(Role).where(Role.code.in_(role_codes))).all()
    for role_row in roles:
        db.execute(insert(user_roles).values(user_id=row.id, role_id=role_row.id))
    response = public_user(db, row)
    write_audit(
        db,
        action="USER_CREATE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=row.id,
        after={
            "username": row.username,
            "display_name": row.display_name,
            "roles": sorted(role_codes),
        },
        **request_meta(request),
    )
    if idempotency_key:
        store(
            db,
            actor_id=auth.user.id,
            endpoint="/admin/users",
            key=idempotency_key,
            payload=body,
            response=response,
        )
    db.commit()
    return response


@router.patch("/users/{user_id}")
def update_user_profile(
    user_id: str,
    payload: UserProfileInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
) -> dict:
    user = _get_user(db, user_id)
    _assert_can_manage_target(db, auth, user)
    before = {"display_name": user.display_name}
    user.display_name = payload.display_name
    write_audit(
        db,
        action="USER_PROFILE_UPDATE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=user.id,
        before=before,
        after={"display_name": user.display_name},
        **request_meta(request),
    )
    db.commit()
    return public_user(db, user)


def _set_roles(
    user_id: str, role_codes: list[str], request: Request, db: Session, auth: AuthContext
) -> dict:
    user = _get_user(db, user_id)
    wanted = set(role_codes)
    if user.id == auth.user.id:
        raise PlatformError(
            "SELF_ROLE_CHANGE_FORBIDDEN", "不能修改自己的角色，请由其他管理员操作", status_code=403
        )
    _assert_can_manage_target(db, auth, user)
    assert_can_grant(db, auth, wanted)
    _lock_users(db)
    before = user_roles_of(db, user.id)
    # 撤销 super_admin 同样需要 super_admin（_assert_can_manage_target 已覆盖）。
    if (
        user.status == "ACTIVE"
        and set(before) & ADMIN_ROLES
        and not wanted & ADMIN_ROLES
        and active_admin_count(db, exclude_user_id=user.id) == 0
    ):
        raise PlatformError(
            "LAST_ADMIN_PROTECTED", "不能移除系统中最后一个有效管理员的管理员角色", status_code=409
        )
    roles = db.scalars(select(Role).where(Role.code.in_(wanted))).all()
    db.execute(delete(user_roles).where(user_roles.c.user_id == user.id))
    for role_row in roles:
        db.execute(insert(user_roles).values(user_id=user.id, role_id=role_row.id))
    changed = sorted(wanted) != before
    if changed:
        invalidate_sessions(db, user)
    write_audit(
        db,
        action="USER_ROLES_UPDATE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=user.id,
        before={"roles": before},
        after={"roles": sorted(wanted), "sessions_revoked": changed},
        **request_meta(request),
    )
    db.commit()
    if changed:
        release_user_leases(user.id, "PERMISSION_CHANGED")
    return {"user_id": user.id, "roles": sorted(wanted), "sessions_revoked": changed}


@router.put("/users/{user_id}/roles")
def set_roles(
    user_id: str,
    payload: RoleAssignInput | list[str],
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("role.manage")),
) -> dict:
    # 兼容旧客户端直接提交 ["viewer"] 数组。
    codes = payload if isinstance(payload, list) else payload.role_codes
    if not codes:
        raise PlatformError("ROLE_REQUIRED", "至少保留一个角色", status_code=400)
    return _set_roles(user_id, codes, request, db, auth)


@router.put("/users/{user_id}/status")
def set_user_status(
    user_id: str,
    payload: UserStatusInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
) -> dict:
    user = _get_user(db, user_id)
    if user.id == auth.user.id:
        raise PlatformError(
            "SELF_STATUS_CHANGE_FORBIDDEN", "不能停用或启用自己的账号", status_code=403
        )
    _assert_can_manage_target(db, auth, user)
    _lock_users(db)
    before = user.status
    if payload.status == before:
        return public_user(db, user)
    if (
        payload.status == "DISABLED"
        and set(user_roles_of(db, user.id)) & ADMIN_ROLES
        and active_admin_count(db, exclude_user_id=user.id) == 0
    ):
        raise PlatformError(
            "LAST_ADMIN_PROTECTED", "不能停用系统中最后一个有效管理员", status_code=409
        )
    user.status = payload.status
    if payload.status == "DISABLED":
        user.deactivated_at = datetime.now(UTC)
        user.deactivated_by = auth.user.id
    else:
        user.deactivated_at = None
        user.deactivated_by = None
        user.failed_attempts = 0
        user.locked_until = None
    invalidate_sessions(db, user)
    write_audit(
        db,
        action="USER_DISABLE" if payload.status == "DISABLED" else "USER_ENABLE",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=user.id,
        before={"status": before},
        after={"status": user.status, "reason": payload.reason, "sessions_revoked": True},
        **request_meta(request),
    )
    db.commit()
    release_user_leases(
        user.id, "USER_DISABLED" if payload.status == "DISABLED" else "USER_ENABLED"
    )
    return public_user(db, user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: str,
    payload: PasswordResetInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("user.manage")),
) -> dict:
    user = _get_user(db, user_id)
    if user.id == auth.user.id:
        raise PlatformError(
            "SELF_RESET_FORBIDDEN", "请通过“修改密码”修改自己的密码", status_code=403
        )
    _assert_can_manage_target(db, auth, user)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    user.failed_attempts = 0
    user.locked_until = None
    invalidate_sessions(db, user)
    write_audit(
        db,
        action="USER_PASSWORD_RESET",
        resource_type="USER",
        user_id=auth.user.id,
        resource_id=user.id,
        after={"must_change_password": True, "sessions_revoked": True},
        **request_meta(request),
    )
    db.commit()
    release_user_leases(user.id, "PASSWORD_RESET")
    return {"user_id": user.id, "must_change_password": True, "sessions_revoked": True}


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
    # P1-02：与 /history/audit 使用同一范围规则，避免旁路读取全局审计。
    from app.modules.history.router import audit_scope_filters

    return [
        serialize_model(x)
        for x in db.scalars(
            select(AuditLog)
            .where(*audit_scope_filters(db, auth))
            .order_by(AuditLog.created_at.desc())
            .limit(min(limit, 2000))
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
