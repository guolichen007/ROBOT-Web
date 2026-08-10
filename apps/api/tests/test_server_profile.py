import pytest
from app.core.config import Settings


def safe_server_settings(**overrides) -> Settings:
    values = {
        "app_env": "server",
        "seed_demo": False,
        "mock_enabled": False,
        "mqtt_allow_anonymous": False,
        "cookie_secure": True,
        "jwt_secret": "j" * 40,
        "refresh_secret": "r" * 40,
        "csrf_secret": "c" * 40,
        "bootstrap_admin_password": "Bootstrap-Server-Secret-2026!",
        "mqtt_username": "platform",
        "mqtt_password": "mqtt-secret",
        "mqtt_tls": True,
        "mqtt_port": 8883,
        "mqtt_ca_file": "/run/secrets/mqtt_ca",
    }
    values.update(overrides)
    return Settings(**values)


def test_server_profile_rejects_mock_and_demo() -> None:
    settings = safe_server_settings(seed_demo=True)
    with pytest.raises(RuntimeError, match="forbids demo seed"):
        settings.assert_profile_safety()


def test_server_profile_requires_verified_mqtt_tls() -> None:
    settings = safe_server_settings(mqtt_tls=False, mqtt_port=1883, mqtt_ca_file=None)
    with pytest.raises(RuntimeError, match="MQTT TLS"):
        settings.assert_profile_safety()


def test_safe_server_profile_passes() -> None:
    safe_server_settings().assert_profile_safety()
