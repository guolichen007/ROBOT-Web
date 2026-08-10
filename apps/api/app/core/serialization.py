from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.inspection import inspect


def serialize_model(obj: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr in inspect(obj).mapper.column_attrs:
        value = getattr(obj, attr.key)
        result[attr.key] = value.isoformat() if isinstance(value, datetime | date) else value
    return result
