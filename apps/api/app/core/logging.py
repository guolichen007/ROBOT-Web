from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from app.core.config import get_settings


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", self.service),
            "message": record.getMessage(),
        }
        for field in (
            "request_id",
            "correlation_id",
            "vehicle_id",
            "command_id",
            "task_id",
            "event_id",
            "operator_id",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(service: str = "api") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(get_settings().log_level.upper())
