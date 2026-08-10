from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "dev"
    app_name: str = "Firebot Cloud Control Platform"
    database_url: str = "postgresql+psycopg://firebot:firebot_dev@postgres:5432/firebot"
    redis_url: str = "redis://redis:6379/0"
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_password_file: str | None = None
    mqtt_allow_anonymous: bool = True
    mqtt_tls: bool = False
    mqtt_ca_file: str | None = None
    mqtt_client_cert_file: str | None = None
    mqtt_client_key_file: str | None = None

    jwt_secret: str = "dev-only-jwt-secret-change-before-server"
    jwt_secret_file: str | None = None
    refresh_secret: str = "dev-only-refresh-secret-change-before-server"
    refresh_secret_file: str | None = None
    csrf_secret: str = "dev-only-csrf-secret-change-before-server"
    csrf_secret_file: str | None = None
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""
    bootstrap_admin_password_file: str | None = None
    bootstrap_admin_display_name: str = "系统管理员"
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost,http://127.0.0.1"
    public_base_url: str = "http://localhost"

    seed_demo: bool = True
    mock_enabled: bool = True
    log_level: str = "INFO"
    asset_root: Path = Path("/data/assets")
    telemetry_retention_days: int = 30
    sensor_retention_days: int = 90
    audit_retention_days: int = 365
    manual_lease_ttl_seconds: int = 5
    command_ack_timeout_seconds: int = 3
    event_stream_maxlen: int = 10_000
    robot_stale_seconds: int = 3
    robot_offline_seconds: int = 10
    login_failure_limit: int = 5
    login_lock_seconds: int = 300
    upload_max_bytes: int = 10 * 1024 * 1024
    ws_ticket_seconds: int = 60
    protocol_schema: Path = Field(
        default=Path("/workspace/packages/protocol-schemas/firebot-message-1.1.schema.json")
    )

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @staticmethod
    def _read_secret(value: str, filename: str | None) -> str:
        if filename and Path(filename).exists():
            return Path(filename).read_text(encoding="utf-8").strip()
        return value

    @property
    def effective_jwt_secret(self) -> str:
        return self._read_secret(self.jwt_secret, self.jwt_secret_file)

    @property
    def effective_refresh_secret(self) -> str:
        return self._read_secret(self.refresh_secret, self.refresh_secret_file)

    @property
    def effective_csrf_secret(self) -> str:
        return self._read_secret(self.csrf_secret, self.csrf_secret_file)

    @property
    def effective_admin_password(self) -> str:
        return self._read_secret(self.bootstrap_admin_password, self.bootstrap_admin_password_file)

    @property
    def effective_mqtt_password(self) -> str | None:
        if self.mqtt_password_file and Path(self.mqtt_password_file).exists():
            return Path(self.mqtt_password_file).read_text(encoding="utf-8").strip()
        return self.mqtt_password

    def configure_mqtt_client(self, client) -> None:
        if not self.mqtt_tls:
            return
        client.tls_set(
            ca_certs=self.mqtt_ca_file,
            certfile=self.mqtt_client_cert_file,
            keyfile=self.mqtt_client_key_file,
        )
        client.tls_insecure_set(False)

    def assert_profile_safety(self) -> None:
        if self.app_env != "server":
            return
        errors: list[str] = []
        if self.seed_demo or self.mock_enabled:
            errors.append("server profile forbids demo seed and Mock Robot")
        if self.mqtt_allow_anonymous:
            errors.append("server profile forbids anonymous MQTT")
        if not self.cookie_secure:
            errors.append("server profile requires secure cookies")
        for name, secret in {
            "JWT": self.effective_jwt_secret,
            "refresh": self.effective_refresh_secret,
            "CSRF": self.effective_csrf_secret,
        }.items():
            if len(secret) < 32 or "dev-only" in secret or "REPLACE" in secret:
                errors.append(f"server profile requires strong {name} secret")
        if len(self.effective_admin_password) < 16:
            errors.append("server profile requires a strong bootstrap admin secret")
        if not self.mqtt_username or not self.effective_mqtt_password:
            errors.append("server profile requires MQTT credentials")
        if not self.mqtt_tls or self.mqtt_port != 8883 or not self.mqtt_ca_file:
            errors.append("server profile requires verified MQTT TLS on port 8883")
        if errors:
            raise RuntimeError("; ".join(errors))


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.assert_profile_safety()
    return settings
