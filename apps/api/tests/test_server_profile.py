from pathlib import Path

import pytest
from app.core.config import Settings


def safe_server_settings(tmp_path: Path, **overrides) -> Settings:
    database_url_file = tmp_path / "database_url"
    database_url_file.write_text(
        "postgresql+psycopg://firebot:strong-password@postgres:5432/firebot", encoding="utf-8"
    )
    redis_url_file = tmp_path / "redis_url"
    redis_url_file.write_text("redis://:strong-password@redis:6379/0", encoding="utf-8")
    values = {
        "app_env": "server",
        "seed_demo": False,
        "mock_enabled": False,
        "mqtt_allow_anonymous": False,
        "cookie_secure": True,
        "enable_api_docs": False,
        "database_url_file": str(database_url_file),
        "redis_url_file": str(redis_url_file),
        "jwt_secret": "j" * 40,
        "refresh_secret": "r" * 40,
        "csrf_secret": "c" * 40,
        "bootstrap_admin_password": "Bootstrap-Server-Secret-2026!",
        "mqtt_username": "platform",
        "mqtt_password": "mqtt-secret",
        "media_publish_token": "m" * 40,
        "mqtt_tls": True,
        "mqtt_port": 8883,
        "mqtt_ca_file": "/run/secrets/mqtt_ca",
    }
    values.update(overrides)
    return Settings(**values)


def test_server_profile_rejects_mock_and_demo(tmp_path: Path) -> None:
    settings = safe_server_settings(tmp_path, seed_demo=True)
    with pytest.raises(RuntimeError, match="forbids demo seed"):
        settings.assert_profile_safety()


def test_server_profile_requires_verified_mqtt_tls(tmp_path: Path) -> None:
    settings = safe_server_settings(tmp_path, mqtt_tls=False, mqtt_port=1883, mqtt_ca_file=None)
    with pytest.raises(RuntimeError, match="MQTT TLS"):
        settings.assert_profile_safety()


def test_safe_server_profile_passes(tmp_path: Path) -> None:
    safe_server_settings(tmp_path).assert_profile_safety()
