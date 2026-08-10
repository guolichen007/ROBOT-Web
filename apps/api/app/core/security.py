from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

password_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def _encode(subject: str, token_type: str, secret: str, expires: timedelta, **extra: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires,
        "jti": str(uuid4()),
        **extra,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def create_access_token(user_id: str, permissions: list[str]) -> str:
    settings = get_settings()
    return _encode(
        user_id,
        "access",
        settings.effective_jwt_secret,
        timedelta(minutes=settings.access_token_minutes),
        permissions=permissions,
    )


def create_refresh_token(user_id: str, family_id: str) -> str:
    settings = get_settings()
    return _encode(
        user_id,
        "refresh",
        settings.effective_refresh_secret,
        timedelta(days=settings.refresh_token_days),
        family_id=family_id,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, get_settings().effective_jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, get_settings().effective_refresh_secret, algorithms=["HS256"])
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def csrf_digest(token: str) -> str:
    key = get_settings().effective_csrf_secret.encode("utf-8")
    return hashlib.sha256(key + token.encode("utf-8")).hexdigest()
