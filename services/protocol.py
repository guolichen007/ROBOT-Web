from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

SCHEMA_VERSION_1_2 = "1.2"
SCHEMA_VERSION_1_3 = "1.3"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION_1_2, SCHEMA_VERSION_1_3)


def _schema_dir() -> Path:
    settings = get_settings()
    if settings.protocol_schema.exists():
        return settings.protocol_schema.parent
    return Path(__file__).resolve().parents[1] / "packages/protocol-schemas"


@lru_cache
def _load_validator(schema_version: str) -> Draft202012Validator:
    """Load the JSON-schema validator for a specific schema version (1.2/1.3)."""
    path = _schema_dir() / f"firebot-message-{schema_version}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"protocol schema not found: {path}")
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def validator_for(schema_version: str) -> Draft202012Validator:
    """Return the validator for the given schema_version.

    Unknown versions are rejected explicitly — never silently downgraded to 1.2.
    """
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValidationError(f"unsupported protocol schema_version: {schema_version!r}")
    return _load_validator(schema_version)


def validate_message(payload: dict[str, Any]) -> None:
    """Validate a message against the schema matching its schema_version."""
    version = str(payload.get("schema_version") or SCHEMA_VERSION_1_2)
    validator_for(version).validate(payload)
