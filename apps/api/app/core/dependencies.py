from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.models import Permission, Role, User, role_permissions, user_roles
from app.db.session import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


@dataclass
class AuthContext:
    user: User
    permissions: set[str]


def load_permissions(db: Session, user_id: str) -> set[str]:
    rows = db.execute(
        select(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(Role, Role.id == role_permissions.c.role_id)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(user_roles.c.user_id == user_id)
    ).scalars()
    return set(rows)


def get_auth_context(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> AuthContext:
    try:
        payload = decode_access_token(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或过期的访问令牌"
        ) from exc
    user = db.get(User, str(payload["sub"]))
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不可用")
    allowed_during_password_change = {
        "/api/v1/auth/me",
        "/api/v1/auth/change-password",
        "/api/v1/auth/logout",
    }
    if user.must_change_password and request.url.path not in allowed_during_password_change:
        raise HTTPException(status_code=428, detail="首次登录必须先修改初始密码")
    return AuthContext(user=user, permissions=load_permissions(db, user.id))


CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]
DbSession = Annotated[Session, Depends(get_db)]


def require_permission(code: str) -> Callable:
    def dependency(auth: CurrentAuth) -> AuthContext:
        if code not in auth.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"缺少权限: {code}")
        return auth

    return dependency


def request_meta(request: Request) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
