from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from jsonschema import Draft202012Validator, FormatChecker


@lru_cache
def validator() -> Draft202012Validator:
    path = get_settings().protocol_schema
    if not path.exists():
        path = (
            Path(__file__).resolve().parents[1]
            / "packages/protocol-schemas/firebot-message-1.2.schema.json"
        )
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def validate_message(payload: dict[str, Any]) -> None:
    validator().validate(payload)
