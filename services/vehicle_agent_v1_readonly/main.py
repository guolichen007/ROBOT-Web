"""vehicle-agent-v1-readonly 入口。

用法（仓库根目录）：
    PYTHONPATH=. python3 -m services.vehicle_agent_v1_readonly.main \
        --config services/vehicle-agent-v1-readonly/config/<设备>.json

生命周期：加载身份（单一配置源）→ 单 owner 守卫 → 连接/重连 → 周期上行 → 优雅退出。
退出码：0 正常退出；2 配置错误；3 owner 冲突；4 连续连接失败放弃。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from services.vehicle_agent_v1_readonly import AGENT_NAME, AGENT_ROLE, AGENT_VERSION
from services.vehicle_agent_v1_readonly.agent import AgentConfig, ReadOnlyAgent
from services.vehicle_agent_v1_readonly.guard import OwnerConflictError, OwnerGuard
from services.vehicle_agent_v1_readonly.identity import IdentityError, load_identity
from services.vehicle_agent_v1_readonly.transport import MqttTransport, TransportConfig

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_OWNER_CONFLICT = 3
EXIT_CONNECT_FAILED = 4


def _default_markers() -> dict[str, tuple[str, ...]]:
    # 001 工控机当前未接入底盘/导航等程序；markers 仅描述"什么进程名算存活"，
    # 对应进程不存在时上报 False，不生成任何模拟值。
    return {
        "chassis": ("firebot_chassis", "chassis_driver"),
        "navigation": ("firebot_navigation", "nav2", "move_base"),
        "patrol": ("firebot_patrol",),
        "thermal": ("firebot_thermal",),
        "battery": ("firebot_battery",),
        "smog": ("firebot_smog",),
        "camera": ("firebot_camera",),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=AGENT_NAME, description=f"Firebot {AGENT_NAME} {AGENT_VERSION} ({AGENT_ROLE})"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="device_identity.json（身份唯一来源）"
    )
    parser.add_argument(
        "--run-dir", type=Path, default=None, help="运行目录（lock 与主机指标日志）"
    )
    parser.add_argument("--status-interval", type=float, default=10.0, help="status 周期（秒）")
    parser.add_argument(
        "--telemetry-interval", type=float, default=30.0, help="telemetry 周期（秒）"
    )
    parser.add_argument(
        "--connect-wait", type=float, default=15.0, help="等待 CONNACK 确认的最长秒数"
    )
    parser.add_argument(
        "--host-metrics",
        choices=["log-only", "status-field"],
        default="log-only",
        help="host 指标默认只写本地日志；status-field 为实验性扩展（默认关闭）",
    )
    parser.add_argument(
        "--allow-placeholder-credentials",
        action="store_true",
        help="仅本地隔离验收允许占位凭证",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger(AGENT_NAME)
    try:
        identity = load_identity(
            args.config, allow_placeholder_password=args.allow_placeholder_credentials
        )
    except IdentityError as exc:
        logger.error("identity rejected: %s", exc)
        return EXIT_CONFIG
    run_dir = (args.run_dir or Path("run") / identity.vehicle_id).resolve()
    guard = OwnerGuard(run_dir)
    try:
        guard.acquire()
    except OwnerConflictError as exc:
        logger.error("refusing to start: %s", exc)
        return EXIT_OWNER_CONFLICT
    stop_event = threading.Event()

    def _request_stop(signum: int, frame: object) -> None:
        logger.info("signal %s received; shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    agent_config = AgentConfig(
        status_interval_seconds=args.status_interval,
        telemetry_interval_seconds=args.telemetry_interval,
        host_metrics_mode=args.host_metrics,
        host_metrics_path=run_dir / "host-metrics.jsonl",
    )
    transport = MqttTransport(identity, TransportConfig(connect_wait_seconds=args.connect_wait))
    agent = ReadOnlyAgent(identity, transport, agent_config, _default_markers(), stop_event)
    logger.info(
        "%s %s starting as %s (display %s); broker %s:%s",
        AGENT_NAME,
        AGENT_VERSION,
        identity.vehicle_id,
        identity.display_name,
        identity.mqtt_host,
        identity.mqtt_port,
    )
    logger.info("safety: no subscription, no hardware output, no velocity command, no bridge.env")
    try:
        return agent.run()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
