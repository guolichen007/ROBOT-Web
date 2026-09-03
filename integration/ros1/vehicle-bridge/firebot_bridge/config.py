"""Firebot 车端 Bridge 配置（环境变量）。"""
from __future__ import annotations

import os
import sys


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "")
    if val.strip() == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """安全 int 解析：非法值（字母 O、空串等）回落默认，绝不抛异常影响启动。"""
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_location_stale_seconds() -> float:
    """location freshness TTL（fail-closed）：0/空/非法一律回落到安全默认 3.0s。

    绝不落入「0=永不过期旧 location」的危险默认——旧位置必须能被清理，
    否则会被重新包装成新 timestamp 上行，污染服务器静止判定。
    """
    raw = os.environ.get("FIREBOT_LOCATION_STALE_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 3.0
    return value if value > 0 else 3.0


class Config:
    """车端 Bridge 全部配置，均来自环境变量（见 config/bridge.env.example）。"""

    # ---- MQTT ----
    mqtt_host: str = os.environ.get("FIREBOT_MQTT_HOST", "100.110.31.112")
    mqtt_port: int = int(os.environ.get("FIREBOT_MQTT_PORT", "8883"))
    # MQTT username 由 DEVICE_ID 派生，不单独手填；空则 validate() 回落到 vehicle_id。
    mqtt_username: str = os.environ.get("FIREBOT_MQTT_USERNAME", "")
    mqtt_password: str = os.environ.get("FIREBOT_MQTT_PASSWORD", "")
    ca_cert: str = os.environ.get("FIREBOT_CA_CERT", "/etc/firebot/production-ca.crt")
    # TLS：生产 true；本地联调测试可设 false（连无 TLS 测试 broker）
    mqtt_tls: bool = _env_bool("FIREBOT_MQTT_TLS", True)

    # ---- 身份 / 地图 ----
    # 设备身份不能有危险默认值：缺失即启动失败（firebotctl enroll 写入 /etc/firebot/device.env）。
    # FIREBOT_DEVICE_ID 是 Fleet 化的唯一人工输入根；FIREBOT_VEHICLE_ID 作为兼容别名。
    vehicle_id: str = os.environ.get("FIREBOT_VEHICLE_ID", os.environ.get("FIREBOT_DEVICE_ID", ""))
    # 地图身份不设危险默认值：必须显式配置，且 location 默认关闭。
    site_code: str = os.environ.get("FIREBOT_SITE_CODE", "")
    map_code: str = os.environ.get("FIREBOT_MAP_CODE", "")
    map_version: str = os.environ.get("FIREBOT_MAP_VERSION", "")
    map_checksum: str = os.environ.get("FIREBOT_MAP_CHECKSUM", "")
    # location 上行门控：地图身份真实确认前绝不发布 canonical location。
    location_enabled: bool = _env_bool("FIREBOT_LOCATION_ENABLED", False)

    # ---- 上报频率 ----
    heartbeat_hz: float = _env_float("FIREBOT_HEARTBEAT_HZ", 1.0)
    status_hz: float = _env_float("FIREBOT_STATUS_HZ", 1.0)
    location_max_hz: float = _env_float("FIREBOT_LOCATION_MAX_HZ", 10.0)

    # ---- 控制占位语义 ----
    # False=生产（命令只转发 ROS placeholder，无 feedback 时回 rejected/BRIDGE_ADAPTER_NOT_CONNECTED）
    # True=联调（测试适配器，可临时声明测试命令、可模拟 feedback）
    bridge_stub_mode: bool = _env_bool("BRIDGE_STUB_MODE", False)
    # ROS feedback 等待超时（秒）；超时无 feedback 视为 ROS adapter 未接
    feedback_timeout_seconds: float = _env_float("FIREBOT_FEEDBACK_TIMEOUT_SECONDS", 3.0)
    # stub 联调：是否模拟 feedback
    stub_simulate_feedback: bool = _env_bool("BRIDGE_STUB_SIMULATE_FEEDBACK", True)
    # stub 模拟类型：rejected(默认，证明闭环不假装执行) | accepted
    stub_feedback_simulation: str = os.environ.get("BRIDGE_STUB_FEEDBACK_SIMULATION", "rejected")

    # ---- 能力声明（唯一权威 = 真实可接受执行的能力）----
    # 生产默认不声明任何命令（未接 ROS 实现）；stub 模式可临时声明测试命令。
    supported_commands: list = [
        item.strip()
        for item in os.environ.get("FIREBOT_SUPPORTED_COMMANDS", "").split(",")
        if item.strip()
    ]
    # 传感器能力：真实拥有的传感器（唯一权威）。无 smoke 源则不声明 smoke。
    sensors: list = [
        item.strip()
        for item in os.environ.get("FIREBOT_SENSORS", "").split(",")
        if item.strip()
    ]
    media: list = [
        item.strip()
        for item in os.environ.get("FIREBOT_MEDIA", "").split(",")
        if item.strip()
    ]
    protocol_version: str = os.environ.get("FIREBOT_PROTOCOL_VERSION", "1.3.0")

    # ---- 现场通信链路追踪（纯 observability，不参与任何业务/协议/控制）----
    # 只控制 telemetry 是否刷 journal/verbose；critical/important 事件恒记录，不受此开关影响。
    field_trace_enabled: bool = _env_bool("FIREBOT_FIELD_TRACE", False)

    # ---- 可观测事件持久化（Event/Audit/Telemetry）----
    # events.jsonl / telemetry.jsonl 落盘目录；空则禁用文件持久化（fail-open，不影响控制）。
    events_dir: str = os.environ.get("FIREBOT_EVENTS_DIR", "")
    # telemetry 持久化开关（默认 true）；与 FIREBOT_FIELD_TRACE（journal 刷屏）解耦。
    telemetry_log_enabled: bool = _env_bool("FIREBOT_TELEMETRY_LOG_ENABLED", True)
    event_log_max_bytes: int = _env_int("FIREBOT_EVENT_LOG_MAX_BYTES", 10 * 1024 * 1024)
    event_log_max_age_hours: float = _env_float("FIREBOT_EVENT_LOG_MAX_AGE_HOURS", 24.0)
    event_log_keep: int = _env_int("FIREBOT_EVENT_LOG_KEEP", 14)
    telemetry_log_keep: int = _env_int("FIREBOT_TELEMETRY_LOG_KEEP", 7)
    event_log_max_total_bytes: int = _env_int("FIREBOT_EVENT_LOG_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024)
    event_log_min_free_bytes: int = _env_int("FIREBOT_EVENT_LOG_MIN_FREE_BYTES", 1 * 1024 * 1024 * 1024)
    event_queue_size: int = _env_int("FIREBOT_EVENT_QUEUE_SIZE", 20000)
    battery_source: str = os.environ.get("FIREBOT_BATTERY_SOURCE", "UNKNOWN")
    smoke_source: str = os.environ.get("FIREBOT_SMOKE_SOURCE", "UNKNOWN")
    # freshness guard：provider 断源后 stale 清除的 TTL（秒）。0 或空 = 未启用（不清除）。
    # 真实 provider 发布周期尚未知，此处仅测试 TTL，不是 01 实车生产最终值。
    battery_stale_seconds: float = _env_float("FIREBOT_BATTERY_STALE_SECONDS", 0.0)
    smoke_stale_seconds: float = _env_float("FIREBOT_SMOKE_STALE_SECONDS", 0.0)
    # location freshness TTL（fail-closed）：0/空/非法 → 3.0s，绝不允许「永不过期旧 location」。
    location_stale_seconds: float = _env_location_stale_seconds()

    # ---- 身份/密码缺失即退出（fail-closed，绝不落入危险默认身份） ----
    def validate(self) -> None:
        if not self.vehicle_id:
            print(
                "ERROR: FIREBOT_VEHICLE_ID/FIREBOT_DEVICE_ID 未设置（设备身份必须显式，"
                "由 firebotctl vehicle enroll 生成 /etc/firebot/device.env）",
                file=sys.stderr,
            )
            sys.exit(1)
        if not self.mqtt_username:
            self.mqtt_username = self.vehicle_id
        if not self.mqtt_password:
            print(
                "ERROR: FIREBOT_MQTT_PASSWORD 未设置（应从 /etc/firebot/bridge-secret.env 或 "
                "systemd EnvironmentFile 注入）",
                file=sys.stderr,
            )
            sys.exit(1)
        if self.mqtt_tls and not os.path.exists(self.ca_cert):
            print(f"ERROR: CA 文件不存在: {self.ca_cert}", file=sys.stderr)
            sys.exit(1)
        if self.bridge_stub_mode:
            print(
                "WARN: BRIDGE_STUB_MODE=true（联调模式，可临时声明测试命令/模拟反馈；"
                "生产必须为 false）",
                file=sys.stderr,
            )
        if self.location_enabled and not (
            self.site_code and self.map_code and self.map_version and self.map_checksum
        ):
            print(
                "ERROR: FIREBOT_LOCATION_ENABLED=true 但地图身份未完整配置"
                "（SITE_CODE/MAP_CODE/MAP_VERSION/MAP_CHECKSUM）",
                file=sys.stderr,
            )
            sys.exit(1)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.validate()
    return _config
